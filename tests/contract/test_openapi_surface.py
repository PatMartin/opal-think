"""Generated OpenAPI surface must match the ratified contract exactly.

The contract YAML is authoritative: every path+method it documents must exist in the
FastAPI-generated OpenAPI, and the service must not grow undocumented public routes.
Also asserts key status/parameter/schema constraints wired into the implementation.
"""

from __future__ import annotations

from importlib import resources

import yaml
from fastapi.testclient import TestClient


def _contract() -> dict:
    """Contract spec with kernel pathItem ``$ref``s resolved (settings/health surface)."""
    pkg = resources.files("opal_think").joinpath("contract")
    spec = yaml.safe_load(pkg.joinpath("opal_think.yaml").read_text(encoding="utf-8"))
    kernel = yaml.safe_load(pkg.joinpath("shared/opal_kernel.yaml").read_text(encoding="utf-8"))
    path_items = kernel.get("components", {}).get("pathItems", {})
    for path, item in spec["paths"].items():
        if "$ref" in item:
            resolved = path_items[item["$ref"].rsplit("/", 1)[1]]
            spec["paths"][path] = {**resolved, **{k: v for k, v in item.items() if k != "$ref"}}
    return spec


def _generated(client: TestClient) -> dict:
    return client.app.openapi()


def _ops(spec: dict) -> set[tuple[str, str]]:
    return {
        (method, path)
        for path, item in spec["paths"].items()
        for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    }


def test_all_contract_operations_present(client: TestClient) -> None:
    contract_ops = _ops(_contract())
    generated_ops = _ops(_generated(client))
    assert len(contract_ops) == 44, f"contract inventory drifted: {len(contract_ops)} ops"
    assert contract_ops == generated_ops, (
        f"missing: {sorted(contract_ops - generated_ops)}; undocumented extras: {sorted(generated_ops - contract_ops)}"
    )


def test_route_inventory_detects_missing_operation(client: TestClient) -> None:
    """Negative check: a tampered contract (dropped path) must mismatch the generated spec."""
    spec = _contract()
    spec["paths"].pop("/api/v1/task/{task_id}/cancel")
    assert _ops(spec) != _ops(_generated(client))


def test_key_status_and_params(client: TestClient) -> None:
    paths = _generated(client)["paths"]
    create = paths["/api/v1/task/"]["post"]
    assert "202" in create["responses"]
    assert any(p["name"] == "Idempotency-Key" and p["in"] == "header" for p in create.get("parameters", []))
    assert "201" in paths["/api/v1/session/"]["post"]["responses"]
    assert "204" in paths["/api/v1/session/{session_id}"]["delete"]["responses"]
    stream = paths["/api/v1/task/{task_id}/stream"]["get"]
    assert "200" in stream["responses"]
    cancel = paths["/api/v1/task/{task_id}/cancel"]["post"]
    assert "202" in cancel["responses"]
    # error statuses (404/409/412/428/429/503) are raised OpalHTTPException, not declared
    # response models; they are verified behaviorally in the contract tests
    assert "503" in paths["/api/v1/health/ready"]["get"]["responses"]
    assert "201" in paths["/api/v1/persona/"]["post"]["responses"]
    assert "204" in paths["/api/v1/persona/{persona_id}"]["delete"]["responses"]
    assert "200" in paths["/api/v1/info/"]["get"]["responses"]
    assert "200" in paths["/api/v1/info/ping"]["get"]["responses"]


def test_key_schema_constraints(client: TestClient) -> None:
    schemas = _generated(client)["components"]["schemas"]
    task_create = schemas["TaskCreate"]
    assert "prompt" in task_create["required"]
    assert "metadata" in task_create["properties"]
    task = schemas["Task"]
    for field in ("id", "status", "prompt", "steps", "steps_completed", "answer", "error", "cancel_requested"):
        assert field in task["properties"]
    assert set(schemas["TaskStatus"]["enum"]) == {"queued", "running", "completed", "failed", "cancelled", "timed_out"}
    health = schemas["Health"]
    for field in ("status", "ready", "models_available", "event", "active_tasks", "pending_restart_fields"):
        assert field in health["properties"]


def test_generated_openapi_parses_and_reports(client: TestClient) -> None:
    spec = _generated(client)
    assert spec["openapi"].startswith("3.1") or spec["openapi"].startswith("3.0")
    assert spec["info"]["title"] == "OPAL Think API"
    assert spec["info"]["version"] == "1.7.1"
