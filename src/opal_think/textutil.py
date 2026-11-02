"""User-facing text hygiene: hidden chain-of-thought never reaches answers or streams.

Thinking-style models (qwen3, deepseek-r1…) may emit ``<think>…</think>`` blocks inside
content. The contract requires user-visible output to exclude hidden reasoning, so we
strip those blocks from both final answers and incremental stream deltas.
"""

from __future__ import annotations

import re

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_OPEN, _CLOSE = "<think>", "</think>"


def strip_think_blocks(text: str) -> str:
    """Remove complete <think>…</think> blocks and any unterminated trailing block."""
    stripped = _THINK_RE.sub("", text)
    if (idx := stripped.find(_OPEN)) != -1:
        stripped = stripped[:idx]
    return stripped.strip()


def _partial_tag_tail(buffer: str, tag: str) -> int:
    """Length of the buffer's trailing run that is a proper prefix of ``tag`` (0 if none)."""
    tail_start = buffer.rfind("<")
    if tail_start == -1:
        return 0
    tail = buffer[tail_start:]
    return len(tail) if tag.startswith(tail) else 0


class ThinkStripper:
    """Stateful streaming filter for <think> blocks across arbitrary delta boundaries.

    Feed deltas in; emit only user-facing text. Tags split across chunks are handled by
    holding back possible tag prefixes at the buffer tail.
    """

    def __init__(self) -> None:
        self._inside = False
        self._buffer = ""

    def feed(self, delta: str) -> str:
        self._buffer += delta
        out: list[str] = []
        while self._buffer:
            if self._inside:
                end = self._buffer.find(_CLOSE)
                if end != -1:
                    self._buffer = self._buffer[end + len(_CLOSE) :]
                    self._inside = False
                    continue
                if _partial_tag_tail(self._buffer, _CLOSE):
                    break  # wait: buffer tail may be the start of a close tag
                self._buffer = self._buffer[-len(_CLOSE) :]  # keep only a tail check window
                break
            start = self._buffer.find(_OPEN)
            if start == -1:
                hold = _partial_tag_tail(self._buffer, _OPEN)
                emit_len = len(self._buffer) - hold
                out.append(self._buffer[:emit_len])
                self._buffer = self._buffer[emit_len:]
                break
            out.append(self._buffer[:start])
            self._buffer = self._buffer[start:]
            if self._buffer.startswith(_OPEN):
                self._inside = True
                self._buffer = self._buffer[len(_OPEN) :]
            else:
                break  # buffer tail is a partial "<think"; wait for more input
        return "".join(out)

    def flush(self) -> str:
        if self._inside:
            return ""
        tail, self._buffer = self._buffer, ""
        return strip_think_blocks(tail) if _OPEN in tail else tail
