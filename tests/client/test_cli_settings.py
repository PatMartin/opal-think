"""CLI ``settings`` commands against the real app via a MockTransport bridge.

The bridge forwards the CLI's real ApiClient requests through starlette's TestClient, so
request construction, ETag flow, and error handling are exercised end to end.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from opal_think.app import create_app
from opal_think.cli import main as cli_main
from opal_think.cli.client import ApiClient
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


def test_settings_list(api) -> None:
    result = runner.invoke(cli_main.think_app, ["settings", "list"])
    assert result.exit_code == 0, result.output
    assert "task.max_concurrent = 4" in result.output


def test_settings_list_json(api) -> None:
    result = runner.invoke(cli_main.think_app, ["--json", "settings", "list"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert any(item["key"] == "task.max_concurrent" for item in data["items"])


def test_settings_show(api) -> None:
    result = runner.invoke(cli_main.think_app, ["settings", "show", "task.max_concurrent"])
    assert result.exit_code == 0
    assert json.loads(result.output)["value"] == 4


def test_settings_set_and_remove(api) -> None:
    result = runner.invoke(cli_main.think_app, ["settings", "set", "task.max_concurrent", "8"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["value"] == 8
    # server actually persisted it (proves the ETag auto-fetch worked)
    assert api.get("/api/v1/settings/task.max_concurrent").json()["value"] == 8
    result = runner.invoke(cli_main.think_app, ["settings", "remove", "task.max_concurrent"])
    assert result.exit_code == 0
    assert api.get("/api/v1/settings/task.max_concurrent").json()["value"] == 4


def test_settings_set_string_value(api) -> None:
    result = runner.invoke(cli_main.think_app, ["settings", "set", "models.default_id", "ollama:llama3.2"])
    assert result.exit_code == 0
    assert api.get("/api/v1/settings/models.default_id").json()["value"] == "ollama:llama3.2"


def test_settings_error_exit(api) -> None:
    result = runner.invoke(cli_main.think_app, ["settings", "show", "nope.nope"])
    assert result.exit_code == 1
    assert "unknown setting key" in result.output or "error:" in result.output
