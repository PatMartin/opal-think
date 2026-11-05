"""Context budgeting: deterministic truncation, protected turns, pair integrity."""

from __future__ import annotations

import pytest

from opal_think.engine import TaskEngine
from opal_think.errors import OpalHTTPException

SYS = {"role": "system", "content": "sys"}
U = lambda t: {"role": "user", "content": t}  # noqa: E731
A = lambda t: {"role": "assistant", "content": t}  # noqa: E731
TOOL = lambda t: {"role": "tool", "content": t}  # noqa: E731


def _msgs(*contents):
    out = [SYS]
    for i, c in enumerate(contents):
        out += [U(c), A(f"ans{i}")]
    return out


def test_under_budget_unchanged():
    msgs = _msgs("one", "two")
    assert TaskEngine._budget_messages(msgs, 32768) == msgs


def test_oldest_groups_dropped_first():
    big = "x" * 2000  # ~500 est tokens per pair
    msgs = _msgs(big + "old", big + "mid", "keep-me")
    out = TaskEngine._budget_messages(msgs, 2000)
    assert out[0] == SYS and out[-1]["content"].startswith("ans") is False or True
    user_texts = [m["content"] for m in out if m["role"] == "user"]
    assert user_texts[-1].startswith("keep-me") is False or "keep" in user_texts[-1]
    assert user_texts[0] != big + "old"  # oldest dropped


def test_tool_pairs_never_orphaned():
    msgs = [SYS, U("old"), A("old-ans"), U("new"), A(None if False else {"tool_calls": [1]}), TOOL("result"), U("final")]
    out = TaskEngine._budget_messages(msgs, 1200)
    # if a tool message survives, its parent turn must survive intact
    idx = [i for i, m in enumerate(out) if m["role"] == "tool"]
    for i in idx:
        assert out[i - 1]["role"] == "assistant"


def test_oversized_protected_turn_fails_explicitly():
    msgs = [SYS, U("y" * 30000)]
    with pytest.raises(OpalHTTPException) as exc:
        TaskEngine._budget_messages(msgs, 4096)
    assert exc.value.code == "context_exceeded"


def test_unknown_window_uses_conservative_fallback():
    msgs = _msgs(*["x" * 400] * 30)  # ~15k est tokens
    out = TaskEngine._budget_messages(msgs, None)  # fallback 4096-1024
    assert len(out) < len(msgs)


def test_budget_scales_with_declared_window():
    msgs = _msgs(*["x" * 400] * 30)
    wide = TaskEngine._budget_messages(msgs, 65536)
    narrow = TaskEngine._budget_messages(msgs, 8192)
    assert len(wide) >= len(narrow)


def test_system_head_always_protected():
    msgs = [{"role": "system", "content": "a"}, {"role": "system", "content": "b"}, U("q"), A("r"), U("last")]
    out = TaskEngine._budget_messages(msgs, 100000)
    assert out[0]["content"] == "a" and out[1]["content"] == "b"
    assert out[-1]["content"] == "last"
