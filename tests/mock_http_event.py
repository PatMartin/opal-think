"""``mock_http_event``: released-contract EVENT fixture over ``httpx.MockTransport``.

Provenance: this fixture models the released ``opal_event_v1.6.0.yaml`` surface that
THINK consumes (verified against ``opal-event`` @ contract 1.6.0 at implementation
time):

- ``GET /api/v1/action/`` -> 200 ``ActionList`` ``[...]``
- ``POST /api/v1/action/{action_id}/execute`` body ``{event: {...}}`` -> 202
  ``Execution`` admission record (queued; admission is not outcome)
- ``GET /api/v1/execution/{execution_id}`` -> 200 ``Execution`` (status enum:
  queued, running, succeeded, failed, timed_out, dropped)
- errors carry the ``OpalError`` envelope ``{error: {code, message, request_id,
  transaction_id, session_id?, causation_id?}}``; ``X-Opal-*`` context headers are
  echoed and a fresh ``X-Opal-Request-Id`` is minted per hop

Optional action metadata (``description``, ``input_schema``, ``keywords``,
``suggested_prompts``) follows the self-describing-actions convention: declared
``input_schema`` is enforced server-side on ``event.data``, mirroring real EVENT
validation of the ``EventInput`` envelope (``topic`` pattern included).

Everything stays in-process on the mock transport — no sockets, shells, or
subprocesses. ``requests`` records the full wire exchange for assertions.
"""

from __future__ import annotations

import itertools
import json
import re
import uuid
from typing import Any

import httpx
import jsonschema

from opal_think.event_client import HttpEventClient

PROVENANCE = "mock_http_event"
CONTRACT_SOURCE = "opal_event_v1.6.0.yaml"

_BASE_URL = "http://event.test"
_TOPIC_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)*$")
_CORRELATION_HEADERS = {
    "x-opal-session-id": "X-Opal-Session-Id",
    "x-opal-transaction-id": "X-Opal-Transaction-Id",
    "x-opal-causation-id": "X-Opal-Causation-Id",
}

# Released-contract action shape (ShellAction); callers may pass any catalog.
DEFAULT_ACTIONS: list[dict[str, Any]] = [
    {
        "id": "gpu_diagnostics",
        "type": "shell",
        "enabled": True,
        "mode": "exec",
        "executable": "/opt/opal/bin/gpu-diag",
        "args": [],
        "stdin": "event_json",
        "timeout_seconds": 30,
    }
]


class MockEventApi:
    """In-process EVENT-shaped HTTP API; drive it through a real HttpEventClient."""

    provenance = PROVENANCE

    def __init__(self, actions: list[dict[str, Any]] | None = None, *, available: bool = True) -> None:
        self.actions = actions if actions is not None else list(DEFAULT_ACTIONS)
        self.available = available
        self.requests: list[dict[str, Any]] = []  # every wire exchange, in order
        self.executions: dict[str, dict[str, Any]] = {}  # admitted executions by id
        self._terminals: dict[str, dict[str, Any]] = {}  # action_id -> terminal fields
        self._pending: dict[str, list[str]] = {}  # action_id -> non-terminal statuses served first
        self._execute_overrides: dict[str, Any] = {}  # action_id -> int status or raw JSON payload
        self.poll_failures = 0  # polls answered 503 before succeeding (transient)
        self.forced_poll_status: str | None = None  # every poll returns this status
        self._exec_seq = itertools.count(1)
        self._req_seq = itertools.count(1)

    # -- configuration ------------------------------------------------------

    def set_terminal(self, action_id: str, **fields: Any) -> None:
        """Terminal Execution fields EVENT will eventually report for an action."""
        self._terminals[action_id] = fields

    def set_pending(self, action_id: str, *statuses: str) -> None:
        """Non-terminal statuses served on successive polls before the terminal record."""
        self._pending[action_id] = list(statuses)

    def set_execute_override(self, action_id: str, override: int | Any) -> None:
        """Force execute's response: int -> that status + OpalError; otherwise raw 202 JSON body."""
        self._execute_overrides[action_id] = override

    def client(self, credential_ref: str | None = None) -> HttpEventClient:
        """A real HTTP adapter pointed at this mock over MockTransport."""
        return HttpEventClient(_BASE_URL, credential_ref, transport=httpx.MockTransport(self.handler))

    @property
    def calls(self) -> list[dict[str, Any]]:
        """Admitted executions in FakeEventClient.calls shape for shared assertions."""
        return [
            {"action_id": r["action_id"], "event": r["event"], "execution_id": r["id"]}
            for r in self.executions.values()
        ]

    # -- transport handler ----------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        try:
            body = json.loads(request.content) if request.content else None
        except ValueError:
            body = "<unparseable>"
        self.requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "headers": dict(request.headers),
                "json": body,
            }
        )
        response = self._route(request, body)
        entry = self.requests[-1]
        entry["status"] = response.status_code
        try:
            entry["response_json"] = response.json()
        except ValueError:
            entry["response_json"] = None
        return response

    def _route(self, request: httpx.Request, body: Any) -> httpx.Response:
        if not self.available:
            return self._error(503, "unavailable", "service unavailable", request)
        path = request.url.path
        if request.method == "GET" and path == "/api/v1/action/":
            return self._json(200, list(self.actions), request)
        if request.method == "POST" and (m := re.fullmatch(r"/api/v1/action/([^/]+)/execute", path)):
            return self._execute(m.group(1), body, request)
        if request.method == "GET" and (m := re.fullmatch(r"/api/v1/execution/([^/]+)", path)):
            return self._poll(m.group(1), request)
        return self._error(404, "not_found", f"resource does not exist: {path}", request)

    def _execute(self, action_id: str, body: Any, request: httpx.Request) -> httpx.Response:
        action = next((a for a in self.actions if a.get("id") == action_id), None)
        if action is None:
            return self._error(404, "not_found", f"action '{action_id}' not found", request)
        if not action.get("enabled"):
            return self._error(422, "validation_error", f"action '{action_id}' is disabled", request)
        if not isinstance(body, dict) or not isinstance(body.get("event"), dict):
            return self._error(422, "validation_error", "request body must be {event: {...}}", request)
        event = body["event"]
        topic = event.get("topic")
        if topic is not None and not _TOPIC_PATTERN.match(str(topic)):
            return self._error(422, "validation_error", f"event.topic is malformed: {topic!r}", request)
        if isinstance(action.get("input_schema"), dict):
            try:
                jsonschema.validate(event.get("data"), action["input_schema"])
            except jsonschema.ValidationError as exc:
                return self._error(
                    422,
                    "validation_error",
                    f"event.data does not satisfy action '{action_id}' input_schema: {exc.message}",
                    request,
                )
        if action_id in self._execute_overrides:
            override = self._execute_overrides[action_id]
            if isinstance(override, int):
                return self._error(override, "unavailable" if override >= 500 else "rejected", "forced override", request)
            return self._json(202, override, request)  # raw (possibly malformed) admission
        execution_id = f"exec_{next(self._exec_seq)}"
        record = {
            "id": execution_id,
            "event_id": uuid.uuid4().hex,
            "rule_id": None,
            "action_id": action_id,
            "status": "queued",
            "trigger": "manual",
            "created_at": "2026-01-01T00:00:00Z",
            "action_revision": "r1",
            "error_code": None,
            "error_message": None,
            "event": event,  # fixture bookkeeping, stripped before responses
            "_polls": list(self._pending.get(action_id, ["running"])),
        }
        self.executions[execution_id] = record
        return self._json(202, self._public(record), request)

    def _poll(self, execution_id: str, request: httpx.Request) -> httpx.Response:
        record = self.executions.get(execution_id)
        if record is None:
            return self._error(404, "not_found", f"execution '{execution_id}' not found", request)
        if self.poll_failures > 0:
            self.poll_failures -= 1
            return self._error(503, "unavailable", "transient execution store failure", request)
        if self.forced_poll_status is not None:
            return self._json(200, {**self._public(record), "status": self.forced_poll_status}, request)
        if record["_polls"]:
            status = record["_polls"].pop(0)
            return self._json(200, {**self._public(record), "status": status}, request)
        terminal = {
            **self._public(record),
            "status": "succeeded",
            "result": {"ok": True},
            "finished_at": "2026-01-01T00:00:01Z",
            "duration_ms": 5,
            **self._terminals.get(record["action_id"], {}),
        }
        return self._json(200, terminal, request)

    # -- response helpers -----------------------------------------------------

    @staticmethod
    def _public(record: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in record.items() if not k.startswith("_") and k != "event"}

    def _json(self, status: int, payload: Any, request: httpx.Request) -> httpx.Response:
        response = httpx.Response(status, json=payload)
        self._apply_context(response, request)
        return response

    def _error(self, status: int, code: str, message: str, request: httpx.Request) -> httpx.Response:
        ctx = {k: request.headers.get(h) for k, h in _CORRELATION_HEADERS.items()}
        envelope = {
            "error": {
                "code": code,
                "message": message,
                "request_id": f"req_{next(self._req_seq)}",
                "transaction_id": ctx["x-opal-transaction-id"] or uuid.uuid4().hex,
                "session_id": ctx["x-opal-session-id"],
                "causation_id": ctx["x-opal-causation-id"],
            }
        }
        return self._json(status, envelope, request)

    @staticmethod
    def _apply_context(response: httpx.Response, request: httpx.Request) -> None:
        response.headers["X-Opal-Request-Id"] = uuid.uuid4().hex
        for incoming, outgoing in _CORRELATION_HEADERS.items():
            if value := request.headers.get(incoming):
                response.headers[outgoing] = value
