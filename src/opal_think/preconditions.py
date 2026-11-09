"""Shared If-Match precondition handling for configuration mutations.

Every mutation of the logical portable configuration (settings, personas) requires a
strong If-Match header: missing -> 428, malformed -> 422, stale -> 412. The revision
comparison itself happens inside the store's write transaction.
"""

from __future__ import annotations

import re

from fastapi import Request

from opal_think.errors import OpalHTTPException

IF_MATCH_PATTERN = re.compile(r'^"[^"]+"$')


def require_if_match(request: Request) -> str:
    """If-Match must be exactly one strong quoted ETag: missing -> 428,
    weak tags/lists/``*``/unquoted -> 422."""
    raw = request.headers.get("If-Match")
    if raw is None:
        raise OpalHTTPException(428, "precondition_required", "If-Match header is required for this mutation")
    raw = raw.strip()
    if not IF_MATCH_PATTERN.match(raw):
        raise OpalHTTPException(422, "invalid_if_match", "If-Match must be exactly one strong quoted ETag")
    return raw.strip('"')


def if_match_revision(request: Request, prefix: str) -> int:
    """Parse the If-Match precondition into the expected revision integer.

    The comparison itself happens inside the store's write transaction — this
    only enforces header presence and shape (428 missing, 422 malformed, 412
    wrong revision namespace)."""
    supplied = require_if_match(request)
    if not supplied.startswith(prefix):
        raise OpalHTTPException(412, "precondition_failed", "If-Match does not match the current configuration revision")
    try:
        return int(supplied[len(prefix) :])
    except ValueError as exc:
        raise OpalHTTPException(
            412,
            "precondition_failed",
            "If-Match does not match the current configuration revision; re-read and retry",
        ) from exc
