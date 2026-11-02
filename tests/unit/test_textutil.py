"""Unit tests for think-block stripping."""

from __future__ import annotations

import pytest

from opal_think.textutil import ThinkStripper, strip_think_blocks


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("hello", "hello"),
        ("<think>secret</think> hello", "hello"),
        ("<think>a\nb</think>one<think>x</think>two", "onetwo"),
        ("<think>unterminated", ""),
        ("pre <think>mid</think> post <think>open", "pre  post"),
    ],
)
def test_strip_think_blocks(raw: str, clean: str) -> None:
    assert strip_think_blocks(raw) == clean.strip()


def test_stripper_split_tag() -> None:
    s = ThinkStripper()
    assert s.feed("<thi") == ""
    assert s.feed("nk>hid") == ""
    assert s.feed("den</think> yo") == " yo"
    assert s.flush() == ""


def test_stripper_partial_close() -> None:
    s = ThinkStripper()
    s.feed("<think>hid</thi")
    assert s.feed("nk>ok") == "ok"


def test_stripper_plain_passthrough() -> None:
    s = ThinkStripper()
    assert s.feed("plain ") == "plain "
    assert s.feed("text") == "text"
    assert s.flush() == ""
