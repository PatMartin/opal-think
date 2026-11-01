"""Cursor pagination helpers (suite-standard scoped cursors).

Wraps an opaque inner cursor (a keyset ``"<sort>|<id>"`` token) with a filter-set
fingerprint and an explicit expiry: cursors are scoped to the exact filter set and
invalidate on mismatch (422), and cursor values expire after ``CURSOR_TTL_SECONDS``.
Mirrors ``opal_event.pagination``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time

from opal_think.errors import OpalHTTPException

CURSOR_TTL_SECONDS = 3600


def _filters_fingerprint(filters: dict) -> str:
    normalized = json.dumps(filters, sort_keys=True, default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def encode_scoped_cursor(inner_cursor: str, filters: dict) -> str:
    fingerprint = _filters_fingerprint(filters)
    expires_at = int(time.time()) + CURSOR_TTL_SECONDS
    payload = f"{fingerprint}:{expires_at}:{inner_cursor}"
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_scoped_cursor(cursor: str, filters: dict) -> str:
    """Validates and unwraps a scoped cursor; 422 on invalid, mismatched, or expired input."""
    try:
        payload = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        fingerprint, expires_at_str, inner_cursor = payload.split(":", 2)
        expires_at = int(expires_at_str)
    except (ValueError, UnicodeDecodeError) as exc:
        raise OpalHTTPException(422, "invalid_cursor", "invalid pagination cursor") from exc
    if expires_at < int(time.time()):
        raise OpalHTTPException(422, "invalid_cursor", "pagination cursor has expired")
    if fingerprint != _filters_fingerprint(filters):
        raise OpalHTTPException(422, "invalid_cursor", "cursor does not match the current filter set")
    return inner_cursor
