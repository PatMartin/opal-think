"""Deterministic evalset runner: YAML cases -> REST tasks -> assertions.

Evalsets are caller-side per contract ("no evalset management API"): each case
drives the real REST surface with TaskCreate.metadata {evalset, case_id}, a
scripted FakeAdapter, and a FakeEventClient, then asserts the expected task
outcome, step structure, and — critically — what did or did not reach EVENT.

Case YAML fields beyond the basics:

- ``task``:      extra TaskCreate fields (timeout_seconds, max_steps, session_id)
- ``delay``:     fake generation delay in seconds (drives timeout/cancel cases)
- ``cancel_after``: seconds to wait before POSTing /tasks/{id}/cancel
- ``event_unavailable``: EVENT client raises EventUnavailable (model-only mode)
- ``idempotent_replay``: replay the same request with an Idempotency-Key and
  assert the response is the same task record
- ``live``:      opt-in real-model case; requires OPAL_THINK_LIVE_OLLAMA +
  OPAL_THINK_LIVE_MODEL, skipped otherwise and flagged in reports
- ``expect.tool_result_contains``: substrings that must appear in a tool-result
  message fed back to the model (verifies normalized EVENT fields arrive)
- ``expect.error_code``: expected task.error.code for failure cases
- ``expect.replayed``: assert the replayed response carried the same task id
"""

from __future__ import annotations

import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

from opal_think.app import create_app
from opal_think.config import ThinkConfig
from opal_think.errors import OpalHTTPException
from opal_think.event_client import FakeEventClient
from opal_think.providers import Generation, ToolCallRequest

TERMINAL = {"completed", "failed", "cancelled", "timed_out"}


class _UnavailableEventClient:
    """EVENT client whose every call reports the service unreachable."""

    async def list_actions(self, context):
        raise OpalHTTPException(503, "event_unavailable", "EVENT unreachable (simulated)")

    async def execute(self, action_id, event, context):
        raise OpalHTTPException(503, "event_unavailable", "EVENT unreachable (simulated)")

    async def get_execution(self, execution_id, context):
        raise OpalHTTPException(503, "event_unavailable", "EVENT unreachable (simulated)")


def load_cases(evalset_path: Path) -> tuple[str, list[dict[str, Any]]]:
    doc = yaml.safe_load(evalset_path.read_text())
    cases = doc["cases"]
    if doc.get("live"):
        for case in cases:
            case.setdefault("live", True)
    return doc["name"], cases


def live_configured() -> bool:
    return bool(os.environ.get("OPAL_THINK_LIVE_OLLAMA") and os.environ.get("OPAL_THINK_LIVE_MODEL"))


def run_case(evalset_name: str, case: dict[str, Any], tmp_path: Path) -> tuple[dict, list, dict]:
    """Run one case through the real app; returns (task, event_calls, outcome)."""
    app = create_app(ThinkConfig(db_path=tmp_path / f"{case['id']}.db"))
    live = bool(case.get("live"))
    if live:
        app.state.settings.store.set_many(
            {
                "providers.ollama.enabled": True,
                "providers.ollama.base_url": os.environ["OPAL_THINK_LIVE_OLLAMA"],
                "providers.fake.enabled": False,
            }
        )
    else:
        app.state.settings.store.set_many(
            {"providers.ollama.enabled": False, "providers.fake.type": "fake", "providers.fake.enabled": True}
        )
        fake = next(a for a in app.state.providers.adapters() if a.provider_id == "fake")
        if delay := case.get("delay"):
            fake.delay = float(delay)
        script = [
            Generation(
                text=entry.get("text", ""),
                tool_calls=[ToolCallRequest(t["action_id"], t.get("event") or {}) for t in entry.get("tool_calls", [])],
            )
            for entry in case.get("script", [])
        ]
        if script:
            fake.set_script(script)
    event_client = _UnavailableEventClient() if case.get("event_unavailable") else None
    if not live and not case.get("event_unavailable") and (
        "actions" in case or "action_results" in case or "event_reject" in case
    ):
        event_client = FakeEventClient(actions=case.get("actions"))
        for action_id, fields in (case.get("action_results") or {}).items():
            event_client.set_result(action_id, **fields)
        for action_id in case.get("event_reject") or []:
            event_client.reject_ids.add(action_id)
    if event_client is not None:
        app.state.event_client = event_client

    body: dict[str, Any] = {
        "prompt": case["prompt"],
        "model_id": case.get("model_id", "fake:echo" if not case.get("script") else "fake:script"),
        "metadata": {"evalset": evalset_name, "case_id": case["id"]},
    }
    body.update(case.get("task") or {})
    if live:
        body["model_id"] = f"ollama:{os.environ['OPAL_THINK_LIVE_MODEL']}"
    outcome: dict[str, Any] = {"live": live}
    headers = {}
    if case.get("idempotent_replay"):
        headers["Idempotency-Key"] = f"{evalset_name}:{case['id']}"
    with TestClient(app) as client:
        created = client.post("/api/v1/tasks/", json=body, headers=headers)
        assert created.status_code == 202, created.text
        task_id = created.json()["id"]
        if cancel_after := case.get("cancel_after"):
            time.sleep(float(cancel_after))
            client.post(f"/api/v1/tasks/{task_id}/cancel")
        task = _wait_terminal(client, task_id)
        if case.get("idempotent_replay"):
            replayed = client.post("/api/v1/tasks/", json=body, headers=headers)
            outcome["replayed_id"] = replayed.json().get("id") if replayed.status_code == 202 else None
        if not live:
            outcome["tool_messages"] = [
                m["content"]
                for call in fake.calls
                for m in call["messages"]
                if m["role"] == "tool"
            ]
        event_calls = getattr(app.state, "event_client", None)
    return task, event_calls.calls if isinstance(event_client, FakeEventClient) else [], outcome


def _wait_terminal(client: TestClient, task_id: str, seconds: float = 15.0) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/tasks/{task_id}").json()
        if task["status"] in TERMINAL:
            return task
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not reach a terminal state")


def assert_case(
    case: dict[str, Any],
    task: dict[str, Any],
    event_calls: list[dict[str, Any]],
    outcome: dict[str, Any] | None = None,
) -> None:
    expect = case["expect"]
    outcome = outcome or {}
    assert task["status"] == expect["status"], f"{case['id']}: {task['status']} != {expect['status']} (error={task['error']})"
    if expected_text := expect.get("answer_contains"):
        assert expected_text in (task["answer"] or ""), f"{case['id']}: answer {task['answer']!r}"
    if error_code := expect.get("error_code"):
        assert (task["error"] or {}).get("code") == error_code, f"{case['id']}: error={task['error']}"
    assert len(event_calls) == expect.get("event_calls", len(event_calls))
    if "called_actions" in expect:
        assert sorted(c["action_id"] for c in event_calls) == sorted(expect["called_actions"])
    if expect.get("replayed"):
        assert outcome.get("replayed_id") == task["id"], f"{case['id']}: replay returned {outcome.get('replayed_id')}"
    for wanted_text in expect.get("tool_result_contains", []):
        assert any(
            wanted_text in (msg or "") for msg in outcome.get("tool_messages", [])
        ), f"{case['id']}: no tool result contains {wanted_text!r}"
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


def environment_metadata(live_ran: bool) -> dict[str, Any]:
    """Provenance recorded in eval reports: versions, platform, git sha, live model."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=False
        ).stdout.strip()
    except OSError:
        sha = "unknown"
    meta: dict[str, Any] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "git_sha": sha or "unknown",
        "live_ran": live_ran,
    }
    if live_ran:
        meta["live_model"] = os.environ.get("OPAL_THINK_LIVE_MODEL")
        meta["live_endpoint"] = os.environ.get("OPAL_THINK_LIVE_OLLAMA")
        meta["prompt_version"] = "evalsets@HEAD"
    return meta


def generate_report(evalset_dir: Path, tmp_root: Path) -> dict[str, Any]:
    """Run every evalset case and produce the machine-readable summary artifact.

    Live cases are skipped unless configured and marked distinctly so reports
    always distinguish simulated from real-model testing.
    """
    results = []
    live_ran = False
    for path in sorted(evalset_dir.glob("*.yaml")):
        evalset_name, cases = load_cases(path)
        for case in cases:
            if case.get("live") and not live_configured():
                results.append({"evalset": evalset_name, "case_id": case["id"], "outcome": "skipped_live"})
                continue
            try:
                task, event_calls, outcome = run_case(evalset_name, case, tmp_root / f"{evalset_name}-{case['id']}")
                assert_case(case, task, event_calls, outcome)
            except Exception as exc:  # noqa: BLE001 — report records failure, not raises
                results.append({"evalset": evalset_name, "case_id": case["id"], "outcome": "fail", "error": str(exc)[:300]})
            else:
                live_ran = live_ran or outcome.get("live", False)
                results.append(
                    {
                        "evalset": evalset_name,
                        "case_id": case["id"],
                        "outcome": "pass",
                        "task_status": task["status"],
                        "live": outcome.get("live", False),
                    }
                )
    passed = sum(1 for r in results if r["outcome"] == "pass")
    failed = sum(1 for r in results if r["outcome"] == "fail")
    skipped_live = sum(1 for r in results if r["outcome"] == "skipped_live")
    return {
        "generated_by": "opal-think evalset runner",
        "environment": environment_metadata(live_ran),
        "counts": {"total": len(results), "passed": passed, "failed": failed, "skipped_live": skipped_live},
        "cases": results,
    }
