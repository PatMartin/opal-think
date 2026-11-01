"""OPAL Suite cross-cutting request/response correlation headers.

``X-Opal-Session-Id``, ``X-Opal-Transaction-Id``, ``X-Opal-Request-Id``, ``X-Opal-Causation-Id`` are
optional, opaque, 1..128-character printable-ASCII tokens (no whitespace/control characters) that
every request may carry and every response (success or error) must echo back where known. They are
context-propagation only -- never an authentication or authorization mechanism.

- ``transaction_id``: a single logical end-to-end operation; this service assigns one if absent.
- ``request_id``: identifies *this* inbound HTTP exchange; always freshly generated for this hop
  (a client-supplied value is accepted for diagnostics but is not echoed back as this hop's ID).
- ``session_id`` / ``causation_id``: passed through unchanged when supplied.

Malformed values are rejected with the nested ``OpalError`` 422 shape, upstream of routing.
Mirrors ``opal_event.opal_context`` so behavior is identical across the suite.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

_ID_PATTERN = re.compile(r"^[!-~]{1,128}$")

_HEADER_NAMES: dict[str, str] = {
    "session_id": "X-Opal-Session-Id",
    "transaction_id": "X-Opal-Transaction-Id",
    "request_id": "X-Opal-Request-Id",
    "causation_id": "X-Opal-Causation-Id",
}


def is_valid_opal_id(val: object) -> bool:
    """Returns True if val is a valid 1..128 printable-ASCII token without whitespace."""
    return isinstance(val, str) and bool(_ID_PATTERN.match(val))


@dataclass
class OpalContext:
    session_id: str | None
    transaction_id: str
    request_id: str
    causation_id: str | None


def opal_error_body(code: str, message: str, context: OpalContext, details: object = None) -> dict:
    body: dict = {
        "error": {
            "code": code,
            "message": message,
            "request_id": context.request_id,
            "transaction_id": context.transaction_id,
            "session_id": context.session_id,
            "causation_id": context.causation_id,
        }
    }
    if details is not None:
        body["error"]["details"] = details
    return body


def apply_context_headers(response: Response, context: OpalContext) -> None:
    response.headers["X-Opal-Transaction-Id"] = context.transaction_id
    response.headers["X-Opal-Request-Id"] = context.request_id
    if context.session_id is not None:
        response.headers["X-Opal-Session-Id"] = context.session_id
    if context.causation_id is not None:
        response.headers["X-Opal-Causation-Id"] = context.causation_id


def _valid_or_none(val: str | None) -> str | None:
    return val if val is not None and _ID_PATTERN.match(val) else None


class OpalContextMiddleware(BaseHTTPMiddleware):
    """Validates/derives the OPAL correlation context for every request and echoes it on every
    response. Installed as the outermost middleware so it also sees responses produced by FastAPI's
    own exception handlers (OpalError/validation alike)."""

    async def dispatch(self, request: Request, call_next):
        raw = {key: request.headers.get(name) for key, name in _HEADER_NAMES.items()}
        invalid = [
            name for key, name in _HEADER_NAMES.items() if raw[key] is not None and not _ID_PATTERN.match(raw[key])
        ]
        if invalid:
            # Even on a rejected request echo whatever context we *can* trust: malformed values are
            # dropped (not echoed), valid siblings plus fresh transaction/request IDs still are.
            fallback = OpalContext(
                session_id=_valid_or_none(raw["session_id"]),
                transaction_id=_valid_or_none(raw["transaction_id"]) or uuid.uuid4().hex,
                request_id=uuid.uuid4().hex,
                causation_id=_valid_or_none(raw["causation_id"]),
            )
            details = [
                {"header": name, "reason": "must be 1-128 printable ASCII characters with no whitespace/control characters"}
                for name in invalid
            ]
            body = opal_error_body("invalid_header", "One or more OPAL context headers are malformed", fallback, details)
            response = JSONResponse(status_code=422, content=body)
            apply_context_headers(response, fallback)
            return response

        context = OpalContext(
            session_id=raw["session_id"],
            transaction_id=raw["transaction_id"] or uuid.uuid4().hex,
            request_id=uuid.uuid4().hex,
            causation_id=raw["causation_id"],
        )
        request.state.opal_context = context
        response = await call_next(request)
        apply_context_headers(response, context)
        return response


def get_opal_context(request: Request) -> OpalContext:
    """Fetches the context stashed by :class:`OpalContextMiddleware`, with a safe fallback for any
    code path invoked outside a request where the middleware did not run (e.g. direct unit tests)."""
    context = getattr(request.state, "opal_context", None)
    if context is None:
        context = OpalContext(session_id=None, transaction_id=uuid.uuid4().hex, request_id=uuid.uuid4().hex, causation_id=None)
    return context
