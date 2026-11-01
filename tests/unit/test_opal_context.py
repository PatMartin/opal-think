"""Unit tests for the OPAL correlation context rules."""

from __future__ import annotations

import pytest

from opal_think.opal_context import OpalContext, is_valid_opal_id


@pytest.mark.parametrize(
    "value",
    ["a", "task_123", "!@#$%^&*()", "x" * 128],
)
def test_valid_ids(value: str) -> None:
    assert is_valid_opal_id(value)


@pytest.mark.parametrize(
    "value",
    ["", "has space", "tab\tchar", "new\nline", "x" * 129, 123, None, b"bytes"],
)
def test_invalid_ids(value: object) -> None:
    assert not is_valid_opal_id(value)


def test_context_dataclass() -> None:
    ctx = OpalContext(session_id=None, transaction_id="t", request_id="r", causation_id=None)
    assert ctx.session_id is None and ctx.causation_id is None
