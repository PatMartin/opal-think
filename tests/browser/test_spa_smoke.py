"""Opt-in headless browser smoke for the single-file SPA.

Requires OPAL_THINK_BROWSER=1 and a working Playwright install with chromium:
    uv pip install playwright && python -m playwright install chromium
    OPAL_THINK_BROWSER=1 pytest tests/browser/test_spa_smoke.py -v

Runs the real FastAPI app on an ephemeral port (fake provider enabled) and
drives the actual SPA: health banner, model list + detail, ask -> stream ->
answer, task list + detail, session/settings/persona lifecycles (including the
ETag/If-Match write paths and JS prompt()/confirm() dialogs), bearer-token
propagation, and that HTML/JS payloads in prompts and model replies render
inert (text, never executed). External dependency: never counted unless the
env gate is set.
"""

from __future__ import annotations

import contextlib
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


@contextlib.contextmanager
def _dialogs(pg, *answers):
    """Accept JS prompt()/confirm() dialogs in order, answering each with the next value.

    Persona/session/settings mutations are dialog-driven; this plays back scripted
    answers ("" = confirm OK or empty prompt reply) and removes the listener after.
    """
    it = iter(answers)

    def handler(dialog):
        dialog.accept(next(it, ""))

    pg.on("dialog", handler)
    try:
        yield
    finally:
        pg.remove_listener("dialog", handler)


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


def test_session_lifecycle(page) -> None:
    _view(page, "sessions")
    page.wait_for_selector("#sessions-table tbody", timeout=15000)
    with _dialogs(page, "spa session"):
        page.click("#session-new")
        page.wait_for_selector("#sessions-table tbody tr:has-text('spa session')", timeout=15000)
    row = page.locator("#sessions-table tbody tr", has_text="spa session")
    assert row.count() == 1
    with _dialogs(page, ""):  # confirm() -> remove
        row.locator("button", has_text="Remove").click()
        page.wait_for_timeout(2000)
    assert page.locator("#sessions-table tbody tr", has_text="spa session").count() == 0


def test_settings_table_renders(page) -> None:
    _view(page, "settings")
    page.wait_for_selector("#settings-table tbody tr", timeout=15000)
    assert page.locator("#settings-table tbody tr").count() >= 1


def test_settings_edit_and_reset(page) -> None:
    """Inline edit exercises the ETag/If-Match write path end to end."""
    _view(page, "settings")
    page.wait_for_selector("#settings-table tbody tr", timeout=15000)
    row = page.locator("#settings-table tbody tr", has_text="task.max_concurrent")
    with _dialogs(page, "8"):  # prompt("New JSON value...") -> 8
        row.locator("button", has_text="Edit").click()
        page.wait_for_function(
            "() => [...document.querySelectorAll('#settings-table tbody tr')]"
            ".some(r => r.innerText.includes('task.max_concurrent') && r.innerText.includes('8'))",
            timeout=10000,
        )
    with _dialogs(page, ""):  # confirm() -> reset to default
        row.locator("button", has_text="Reset").click()
        page.wait_for_function(
            "() => [...document.querySelectorAll('#settings-table tbody tr')]"
            ".some(r => r.innerText.includes('task.max_concurrent') && !r.innerText.includes('8'))",
            timeout=10000,
        )


def test_tasks_view_and_detail(page) -> None:
    """Task list shows prior asks; clicking a row renders the detail panel."""
    _view(page, "tasks")
    page.wait_for_selector("#tasks-table tbody tr", timeout=15000)
    assert page.locator("#tasks-table tbody tr").count() >= 1
    page.locator("#tasks-table tbody tr").first.click()
    page.wait_for_function(
        "() => document.getElementById('task-detail').innerText.includes('task_')",
        timeout=15000,
    )
    assert "echo:" in page.locator("#task-detail").inner_text()


def test_model_detail_panel(page) -> None:
    _view(page, "models")
    page.wait_for_selector("#models-table tbody tr", timeout=15000)
    page.locator("#models-table tbody tr", has_text="fake:echo").click()
    page.wait_for_function(
        "() => document.getElementById('model-detail').innerText.includes('fake:echo')",
        timeout=15000,
    )


def test_ask_model_select_populated(page) -> None:
    _view(page, "ask")
    page.wait_for_function(
        "() => document.getElementById('ask-model').options.length > 1",
        timeout=15000,
    )
    options = page.locator("#ask-model option").all_inner_texts()
    assert "fake:echo" in options and "fake:script" in options


def test_persona_lifecycle(page) -> None:
    _view(page, "personas")
    page.wait_for_selector("#personas-table tbody", timeout=15000)
    with _dialogs(page, "amy", "Amy", "", "demo persona"):  # id, name, voice, description
        page.click("#persona-new")
        page.wait_for_selector("#personas-table tbody tr:has-text('amy')", timeout=15000)
    row = page.locator("#personas-table tbody tr", has_text="amy")
    assert row.count() == 1
    row.locator("button", has_text="Validate").click()
    page.wait_for_function(
        "() => document.getElementById('persona-detail').innerText.toLowerCase().includes('valid')",
        timeout=15000,
    )
    with _dialogs(page, ""):  # confirm() -> remove
        row.locator("button", has_text="Remove").click()
        page.wait_for_timeout(2000)
    assert page.locator("#personas-table tbody tr", has_text="amy").count() == 0


def test_health_banner_shows_status(page) -> None:
    banner = page.locator("#health-banner").inner_text().lower()
    assert "ok" in banner or "ready" in banner or "model" in banner


def test_auth_token_sent_as_bearer(page) -> None:
    with _dialogs(page, "secret123"):
        page.click("#token-btn")
        page.wait_for_timeout(500)
    assert page.evaluate("sessionStorage.getItem('opal_token')") == "secret123"
    # Reload re-runs SPA init; its API calls must carry the bearer header.
    with page.expect_request(lambda r: r.headers.get("authorization") == "Bearer secret123"):
        page.reload(wait_until="networkidle")
