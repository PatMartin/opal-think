"""Deterministic evalset runner: YAML cases -> REST tasks -> assertions.

Evalsets are caller-side per contract ("no evalset management API"): each case
drives the real REST surface with TaskCreate.metadata {evalset, case_id}, a
scripted FakeAdapter, and a FakeEventClient, then asserts the expected task
outcome, step structure, and — critically — what did or did not reach EVENT.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

from opal_think.app import create_app
from opal_think.config import ThinkConfig
from opal_think.event_client import FakeEventClient
from opal_think.providers import Generation, ToolCallRequest


def load_cases(evalset_path: Path) -> tuple[str, list[dict[str, Any]]]:
    doc = yaml.safe_load(evalset_path.read_text())
    return doc["name"], doc["cases"]


def run_case(evalset_name: str, case: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    """Run one case through the real app; returns (task, event_calls) for assertions."""
    app = create_app(ThinkConfig(db_path=tmp_path / f"{case['id']}.db"))
    app.state.settings.store.set_many(
        {"providers.ollama.enabled": False, "providers.fake.type": "fake", "providers.fake.enabled": True}
    )
    fake = next(a for a in app.state.providers.adapters() if a.provider_id == "fake")
    script = [
        Generation(
            text=entry.get("text", ""),
            tool_calls=[ToolCallRequest(t["action_id"], t.get("event") or {}) for t in entry.get("tool_calls", [])],
        )
        for entry in case.get("script", [])
    ]
    if script:
        fake.set_script(script)
    if "actions" in case or "action_results" in case:
        event = FakeEventClient(actions=case.get("actions"))
        for action_id, fields in (case.get("action_results") or {}).items():
            event.set_result(action_id, **fields)
        app.state.event_client = event
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/tasks/",
            json={
                "prompt": case["prompt"],
                "model_id": case.get("model_id", "fake:echo" if not script else "fake:script"),
                "metadata": {"evalset": evalset_name, "case_id": case["id"]},
            },
        )
        assert created.status_code == 202, created.text
        task_id = created.json()["id"]
        task = _wait_terminal(client, task_id)
        event_calls = getattr(app.state, "event_client", None)
    return task, event_calls.calls if event_calls else []


def _wait_terminal(client: TestClient, task_id: str, seconds: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/tasks/{task_id}").json()
        if task["status"] in {"completed", "failed", "cancelled", "timed_out"}:
            return task
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not reach a terminal state")


def assert_case(case: dict[str, Any], task: dict[str, Any], event_calls: list[dict[str, Any]]) -> None:
    expect = case["expect"]
    assert task["status"] == expect["status"], f"{case['id']}: {task['status']} != {expect['status']} (error={task['error']})"
    if expected_text := expect.get("answer_contains"):
        assert expected_text in (task["answer"] or ""), f"{case['id']}: answer {task['answer']!r}"
    assert len(event_calls) == expect.get("event_calls", len(event_calls))
    if "called_actions" in expect:
        assert sorted(c["action_id"] for c in event_calls) == sorted(expect["called_actions"])
    for wanted in expect.get("steps", []):
        match = next(
            (
                s
                for s in task["steps"]
                if s["kind"] == wanted["kind"]
                and s["status"] == wanted["status"]
                and wanted.get("action_id", s.get("action_id")) == s.get("action_id")
            ),
            None,
        )
        assert match is not None, f"{case['id']}: no step matching {wanted} in {task['steps']}"
        if wanted.get("action_input_null"):
            assert match["action_input"] is None
        elif "action_input" in wanted:
            assert match["action_input"] == wanted["action_input"]
