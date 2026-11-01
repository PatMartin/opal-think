"""CLI ``model`` commands against the real app via the MockTransport bridge."""

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
    test_client = TestClient(create_app(ThinkConfig(db_path=tmp_path / "t.db")))
    test_client.app.state.settings.store.set_many({"providers.fake.type": "fake", "providers.fake.enabled": True})

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


def test_model_list(api) -> None:
    result = runner.invoke(cli_main.think_app, ["model", "list"])
    assert result.exit_code == 0, result.output
    assert "fake:echo" in result.output and "fake:script" in result.output


def test_model_list_json(api) -> None:
    result = runner.invoke(cli_main.think_app, ["--json", "model", "list"])
    assert result.exit_code == 0
    ids = [m["id"] for m in json.loads(result.output)]
    assert "fake:echo" in ids


def test_model_show(api) -> None:
    result = runner.invoke(cli_main.think_app, ["--json", "model", "show", "fake:echo"])
    assert result.exit_code == 0
    assert json.loads(result.output)["provider"] == "fake"


def test_model_show_missing(api) -> None:
    result = runner.invoke(cli_main.think_app, ["model", "show", "fake:nope"])
    assert result.exit_code == 1
    assert "not found" in result.output
