"""OPAL-EVENT REST adapter — the only path by which THINK may act on the world.

THINK holds *execute-scoped* privileges per the ratified design: it lists/shows enabled
actions, invokes ``POST /api/v1/actions/{id}/execute``, and polls ``GET
/api/v1/executions/{id}`` until terminal. It MUST NOT administer actions, rules, settings,
or credentials — this module deliberately exposes no such methods.

Correlation: outbound calls carry a fresh hop request (EVENT mints its own request id),
preserve the task's transaction/session context, and set causation to the calling task.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from opal_think.errors import OpalHTTPException

TERMINAL_EXECUTION_STATUSES = {"succeeded", "failed", "timed_out", "dropped"}
_SENSITIVE_HINTS = ("password", "passwd", "secret", "token", "api_key", "apikey", "credential", "private_key")
_MAX_ACTION_INPUT_BYTES = 4096
_POLL_INTERVAL = 0.5


class EventUnavailable(OpalHTTPException):
    def __init__(self, message: str) -> None:
        super().__init__(503, "event_unavailable", message)


def sanitize_action_input(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Redact secret-looking values and bound size; None when no safe representation exists."""

    def walk(node: Any, depth: int = 0) -> Any:
        if depth > 8:
            return None
        if isinstance(node, dict):
            return {
                k: ("***" if any(h in str(k).lower() for h in _SENSITIVE_HINTS) else walk(v, depth + 1))
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [walk(v, depth + 1) for v in node]
        return node if isinstance(node, (str, int, float, bool)) or node is None else str(node)

    try:
        safe = walk(payload)
        if len(json.dumps(safe, default=str).encode()) > _MAX_ACTION_INPUT_BYTES:
            return None
        return safe
    except (TypeError, ValueError):
        return None


class HttpEventClient:
    """HTTPX adapter over the EVENT contract (actions + executions only)."""

    def __init__(
        self,
        base_url: str,
        credential_ref: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        token_env = credential_ref.removeprefix("env:") if credential_ref else None
        self._token = os.environ.get(token_env) if token_env else None
        self._transport = transport

    def _headers(self, context: dict[str, str | None]) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        for key, header in (
            ("session_id", "X-Opal-Session-Id"),
            ("transaction_id", "X-Opal-Transaction-Id"),
            ("causation_id", "X-Opal-Causation-Id"),
        ):
            if context.get(key):
                headers[header] = context[key]
        return headers

    async def _call(self, method: str, path: str, context: dict, **kwargs: Any) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=10.0, transport=self._transport) as client:
                response = await client.request(method, f"{self._base}{path}", headers=self._headers(context), **kwargs)
        except httpx.HTTPError as exc:
            raise EventUnavailable(f"EVENT unreachable: {exc}") from exc
        if response.status_code == 404:
            raise OpalHTTPException(404, "not_found", f"EVENT resource not found: {path}")
        if response.status_code >= 400:
            raise EventUnavailable(f"EVENT returned HTTP {response.status_code} for {method} {path}")
        return response

    async def list_actions(self, context: dict[str, str | None]) -> list[dict[str, Any]]:
        data = (await self._call("GET", "/api/v1/actions/", context)).json()
        return data.get("items", data) if isinstance(data, dict) else data

    async def execute(self, action_id: str, event: dict[str, Any], context: dict[str, str | None]) -> dict[str, Any]:
        """Enqueue one execution; the 202 Execution record is admission, not outcome."""
        return (
            await self._call("POST", f"/api/v1/actions/{action_id}/execute", context, json={"event": event})
        ).json()

    async def get_execution(self, execution_id: str, context: dict[str, str | None]) -> dict[str, Any]:
        return (await self._call("GET", f"/api/v1/executions/{execution_id}", context)).json()


class FakeEventClient:
    """Deterministic EVENT substitute for tests/evals. Records every call; never touches a network."""

    def __init__(self, actions: list[dict[str, Any]] | None = None) -> None:
        self.actions = actions if actions is not None else [
            {"id": "gpu_diagnostics", "type": "shell", "enabled": True},
            {"id": "remove_gpu_driver", "type": "shell", "enabled": True},
        ]
        self.results: dict[str, dict[str, Any]] = {}  # action_id -> terminal execution fields
        self.calls: list[dict[str, Any]] = []
        self._seq = 0

    def set_result(self, action_id: str, **fields: Any) -> None:
        self.results[action_id] = fields

    async def list_actions(self, context: dict[str, str | None]) -> list[dict[str, Any]]:
        return list(self.actions)

    async def execute(self, action_id: str, event: dict[str, Any], context: dict[str, str | None]) -> dict[str, Any]:
        self._seq += 1
        execution_id = f"exec_{self._seq}"
        self.calls.append({"action_id": action_id, "event": event, "execution_id": execution_id})
        return {"id": execution_id, "action_id": action_id, "status": "queued", "trigger": "manual", "created_at": "t"}

    async def get_execution(self, execution_id: str, context: dict[str, str | None]) -> dict[str, Any]:
        call = next((c for c in self.calls if c["execution_id"] == execution_id), None)
        action_id = call["action_id"] if call else "unknown"
        fields = self.results.get(action_id, {"status": "succeeded", "result": {"ok": True}})
        return {"id": execution_id, "action_id": action_id, "trigger": "manual", "created_at": "t", "finished_at": "t", **fields}


def build_event_client(settings_getter) -> HttpEventClient | None:
    """Client when ``event.base_url`` is configured, else None (model-only mode)."""
    base_url = settings_getter("event.base_url")
    if not base_url:
        return None
    return HttpEventClient(base_url, settings_getter("event.credential_ref"))
