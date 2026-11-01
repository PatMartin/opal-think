"""Unit tests for scoped cursor encode/decode."""

from __future__ import annotations

import base64

import pytest

from opal_think.errors import OpalHTTPException
from opal_think.pagination import decode_scoped_cursor, encode_scoped_cursor


def test_roundtrip() -> None:
    cursor = encode_scoped_cursor("2026-01-01T00:00:00|sess_1", {"status": "completed"})
    assert decode_scoped_cursor(cursor, {"status": "completed"}) == "2026-01-01T00:00:00|sess_1"


def test_filter_mismatch_rejected() -> None:
    cursor = encode_scoped_cursor("key|id", {"status": "completed"})
    with pytest.raises(OpalHTTPException) as exc:
        decode_scoped_cursor(cursor, {"status": "failed"})
    assert exc.value.status_code == 422


def test_expired_rejected() -> None:
    payload = "abc:1:inner"  # expires_at=1 -> long past
    cursor = base64.urlsafe_b64encode(payload.encode()).decode()
    with pytest.raises(OpalHTTPException, match="expired"):
        decode_scoped_cursor(cursor, {})


def test_garbage_rejected() -> None:
    for bad in ("!!!", "aGVsbG8=", "AAAA"):
        with pytest.raises(OpalHTTPException):
            decode_scoped_cursor(bad, {})
