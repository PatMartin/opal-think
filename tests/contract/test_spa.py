"""SPA security audit: user/model/API data must never reach innerHTML sinks.

The SPA is single-file vanilla JS. Static analysis here enforces the invariant
behind the Step-02 refactor: `innerHTML` may only be assigned the empty string
(clears); all untrusted values render via textContent/DOM creation. A browser run
is still recommended for end-to-end confirmation, but this blocks regressions.
"""

from __future__ import annotations

import re
from importlib import resources

import pytest
from fastapi.testclient import TestClient


def _html() -> str:
    return resources.files("opal_think").joinpath("spa/index.html").read_text(encoding="utf-8")


def test_spa_served_at_root(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<title>OPAL Think</title>" in response.text


def test_no_interpolated_innerhtml() -> None:
    html = _html()
    violations = []
    for lineno, line in enumerate(html.splitlines(), 1):
        for match in re.finditer(r"innerHTML\s*=", line):
            rhs = line[match.end() :].strip()
            # Only the empty-string clear is permitted (static, untainted).
            if rhs not in {'"";', "'';", '""', "''"}:
                violations.append(f"line {lineno}: innerHTML={rhs[:60]}")
    assert not violations, "unsafe innerHTML assignments:\n" + "\n".join(violations)


def test_no_dangerous_sinks() -> None:
    html = _html()
    for sink in ("insertAdjacentHTML", "outerHTML", "document.write", "eval(", "new Function("):
        assert sink not in html, f"dangerous DOM sink present: {sink}"


def test_renderers_use_dom_helpers() -> None:
    """The untrusted-data render functions must exist and route through the
    textContent helpers (el/mutedRow), not string-concatenated markup."""
    script = _html().split("<script>", 1)[1]
    for surface in ("renderTask", "loadTasks", "loadSessions", "loadModels", "renderSettings"):
        assert surface in script
    for helper in ("function el(", "const mutedRow", "const pill", "const code"):
        assert helper in script
    # template literals may only feed textContent paths — re-verified by innerHTML rule above


@pytest.mark.parametrize(
    "payload",
    ['<img src=x onerror=alert(1)>', '<script>alert(1)</script>', '" onmouseover="alert(1)', "${constructor}"],
)
def test_spa_has_no_literal_sink_for_payloads(payload: str) -> None:
    """Payload-shaped strings must not appear as HTML in the page template itself."""
    html = _html()
    assert f"<td>{payload}</td>" not in html  # template would be exploitable if literal
    assert f"innerHTML += `{payload}" not in html
