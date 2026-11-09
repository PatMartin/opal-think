"""Deterministic SPA checks that need no browser: JS syntax + API surface."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

SPA = Path(__file__).resolve().parents[2] / "src" / "opal_think" / "spa" / "index.html"


def _script_blocks() -> list[str]:
    html = SPA.read_text(encoding="utf-8")
    return re.findall(r"<script>(.*?)</script>", html, re.DOTALL)


def test_spa_script_parses_with_node() -> None:
    node = shutil.which("node")
    if not node:
        import pytest

        pytest.skip("node executable not available for JS syntax check")
    scripts = _script_blocks()
    assert scripts, "no inline <script> blocks found in SPA"
    for i, block in enumerate(scripts):
        path = Path(tempfile.mkdtemp()) / f"spa_{i}.mjs"
        path.write_text(block, encoding="utf-8")
        result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, f"script block {i} failed node --check:\n{result.stderr[:500]}"


def test_spa_uses_fetch_sse_not_eventsource_for_auth() -> None:
    html = SPA.read_text(encoding="utf-8")
    assert "EventSource" not in html or "cannot attach Authorization" in html
    assert "fetch(" in html


def test_spa_calls_only_contract_endpoints() -> None:
    html = SPA.read_text(encoding="utf-8")
    for path in re.findall(r"api\(`?(/api/v1/[a-zA-Z0-9/_{}$(). ]+?)`?", html):
        assert path.startswith("/api/v1/"), path
    assert "/api/v1/task/" in html and "/api/v1/session/" in html and "/api/v1/setting/" in html
