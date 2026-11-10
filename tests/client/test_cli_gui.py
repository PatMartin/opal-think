"""CLI ``gui`` command: opens the SPA base URL in a browser when the service is up."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from opal_think.app import create_app
from opal_think.cli import main as cli_main
from opal_think.cli.client import ApiCallError, ApiClient
from opal_think.config import ThinkConfig

runner = CliRunner()


@pytest.fixture
def api(tmp_path, monkeypatch):
    """Real app reachable through the CLI's ApiClient over a mock transport."""
    test_client = TestClient(create_app(ThinkConfig(db_path=tmp_path / "t.db")))

    def handler(request: httpx.Request) -> httpx.Response:
        return test_client.request(
            request.method,
            request.url.path + (f"?{request.url.query.decode()}" if request.url.query else ""),
            headers=dict(request.headers),
            content=request.content,
        )

    def make_client(base_url, token=None, timeout=30.0, http=None):
        return ApiClient(base_url, token, timeout, http=httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(cli_main, "ApiClient", make_client)
    return test_client


def test_gui_opens_browser(api, monkeypatch) -> None:
    opened: list[str] = []
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))
    result = runner.invoke(cli_main.think_app, ["gui"])
    assert result.exit_code == 0, result.output
    assert opened == ["http://127.0.0.1:8080"]
    assert "opened http://127.0.0.1:8080" in result.output


def test_gui_service_down(tmp_path, monkeypatch) -> None:
    class DownClient(ApiClient):
        def __init__(self, base_url, token=None, timeout=30.0, http=None):
            super().__init__(base_url, token, timeout, http=httpx.Client(base_url=base_url))

        def get(self, path, **kwargs):  # service unreachable: ping fails fast
            raise ApiCallError(0, "cannot reach OPAL-THINK API")

    monkeypatch.setattr(cli_main, "ApiClient", DownClient)
    opened: list[str] = []
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))
    result = runner.invoke(cli_main.think_app, ["gui"])
    assert result.exit_code == 1
    assert "service unreachable" in result.output
    assert not opened
