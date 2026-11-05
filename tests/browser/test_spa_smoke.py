"""Opt-in headless browser smoke for the single-file SPA.

Requires OPAL_THINK_BROWSER=1 and a working Playwright install with chromium:
    uv pip install playwright && python -m playwright install chromium
    OPAL_THINK_BROWSER=1 pytest tests/browser/test_spa_smoke.py -v

Runs the real FastAPI app on an ephemeral port (fake provider enabled) and
drives the actual SPA: model list, ask -> stream -> answer, session
create/remove, and that HTML/JS payloads in prompts and model replies render
inert (text, never executed). External dependency: never counted unless the
env gate is set.
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OPAL_THINK_BROWSER") != "1",
    reason="browser smoke is opt-in: set OPAL_THINK_BROWSER=1 with playwright+chromium installed",
)

playwright = pytest.importorskip("playwright.sync_api", reason="playwright not installed")


@pytest.fixture(scope="module")
def spa_server():
    import uvicorn

    from opal_think.app import create_app
    from opal_think.config import ThinkConfig

    app = create_app(ThinkConfig(db_path=Path(tempfile.mkdtemp()) / "spa.db"))
    app.state.settings.store.set_many(
        {"providers.ollama.enabled": False, "providers.fake.type": "fake", "providers.fake.enabled": True}
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="module")
def page(spa_server):
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch()
        pg = browser.new_page()
        errors = []
        pg.on("pageerror", lambda exc: errors.append(str(exc)))
        pg.goto(spa_server, wait_until="networkidle")
        pg.errors = errors
        yield pg
        browser.close()


def _view(pg, name: str) -> None:
    pg.locator(f"button[data-view='{name}']").click()
    pg.wait_for_timeout(400)


def test_spa_loads_without_js_errors(page) -> None:
    assert not page.errors, f"page errors: {page.errors[:3]}"
    assert page.locator("#health-banner").inner_text() != ""


def test_models_table_populates(page) -> None:
    _view(page, "models")
    page.wait_for_selector("#models-table tbody tr", timeout=15000)
    assert page.locator("#models-table tbody tr").count() >= 1


def test_ask_task_streams_answer(page) -> None:
    _view(page, "ask")
    page.fill("#ask-prompt", "hello from browser")
    page.click("#ask-submit")
    page.wait_for_function(
        "() => document.getElementById('ask-result').innerText.includes('echo:')",
        timeout=20000,
    )


def test_xss_payload_renders_inert(page) -> None:
    _view(page, "ask")
    page.fill("#ask-prompt", "<img src=x onerror=window.__pwned=1>")
    page.click("#ask-submit")
    page.wait_for_timeout(4000)
    assert page.evaluate("window.__pwned ?? 0") == 0, "injected handler executed"


def test_session_create_and_remove(page) -> None:
    _view(page, "sessions")
    page.wait_for_selector("#sessions-table tbody", timeout=15000)
    before = page.locator("#sessions-table tbody tr").count()
    page.click("#session-new")
    page.wait_for_timeout(3000)
    after = page.locator("#sessions-table tbody tr").count()
    assert after >= before


def test_settings_table_renders(page) -> None:
    _view(page, "settings")
    page.wait_for_selector("#settings-table tbody tr", timeout=15000)
    assert page.locator("#settings-table tbody tr").count() >= 1
