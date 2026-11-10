"""CLI ``persona`` + ``info``/``ping`` commands against the real app via the MockTransport bridge."""

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


def test_version(api) -> None:
    result = runner.invoke(cli_main.think_app, ["info", "version"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["name"] == "opal-think-svc" and body["api_version"] == "1.6.0"


def test_ping(api) -> None:
    result = runner.invoke(cli_main.think_app, ["info", "ping"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"status": "ok"}


def test_persona_lifecycle(api) -> None:
    create = runner.invoke(
        cli_main.think_app,
        ["persona", "create", "amy", "--name", "Amy", "--voice-id", "en-gb-alan-low"],
    )
    assert create.exit_code == 0, create.output
    assert api.get("/api/v1/persona/amy").status_code == 200

    listed = runner.invoke(cli_main.think_app, ["--json", "persona", "list"])
    assert listed.exit_code == 0
    assert [p["persona_id"] for p in json.loads(listed.output)] == ["amy"]

    shown = runner.invoke(cli_main.think_app, ["persona", "show", "amy"])
    assert shown.exit_code == 0
    assert json.loads(shown.output)["voice_id"] == "en-gb-alan-low"

    updated = runner.invoke(cli_main.think_app, ["persona", "update", "amy", "--name", "Amy Prime", "--clear-voice"])
    assert updated.exit_code == 0, updated.output
    body = api.get("/api/v1/persona/amy").json()
    assert body["name"] == "Amy Prime" and body["voice_id"] is None

    removed = runner.invoke(cli_main.think_app, ["persona", "remove", "amy"])
    assert removed.exit_code == 0
    assert api.get("/api/v1/persona/amy").status_code == 404


def test_persona_update_requires_a_field(api) -> None:
    runner.invoke(cli_main.think_app, ["persona", "create", "amy", "--name", "Amy"])
    result = runner.invoke(cli_main.think_app, ["persona", "update", "amy"])
    assert result.exit_code == 1
    assert "nothing to update" in result.output


def test_persona_validate(api) -> None:
    runner.invoke(cli_main.think_app, ["persona", "create", "amy", "--name", "Amy", "--disabled"])
    result = runner.invoke(cli_main.think_app, ["persona", "validate", "amy"])
    assert result.exit_code == 1
    assert "disabled" in result.output
