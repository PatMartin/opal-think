"""CLI ``session`` commands against the real app via the MockTransport bridge."""

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


def test_session_add_list_show_remove(api) -> None:
    result = runner.invoke(cli_main.think_app, ["session", "add", "--title", "demo"])
    assert result.exit_code == 0, result.output
    assert "session_id: sess_" in result.output
    session_id = result.output.split("session_id: ")[1].splitlines()[0]

    result = runner.invoke(cli_main.think_app, ["session", "list"])
    assert result.exit_code == 0 and session_id in result.output

    result = runner.invoke(cli_main.think_app, ["--json", "session", "show", session_id])
    assert result.exit_code == 0
    assert json.loads(result.output)["title"] == "demo"

    result = runner.invoke(cli_main.think_app, ["session", "remove", session_id])
    assert result.exit_code == 0
    assert api.get(f"/api/v1/session/{session_id}").status_code == 404


def test_session_add_json(api) -> None:
    result = runner.invoke(cli_main.think_app, ["--json", "session", "add"])
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert body["id"].startswith("sess_") and body["title"] is None


def test_session_list_pagination_params(api) -> None:
    for _ in range(3):
        runner.invoke(cli_main.think_app, ["session", "add"])
    result = runner.invoke(cli_main.think_app, ["--json", "session", "list", "--limit", "2"])
    assert result.exit_code == 0
    page = json.loads(result.output)
    assert len(page["items"]) == 2 and page["has_more"] is True
    result = runner.invoke(cli_main.think_app, ["--json", "session", "list", "--limit", "2", "--cursor", page["next_cursor"]])
    assert result.exit_code == 0
    assert len(json.loads(result.output)["items"]) == 1


def test_session_remove_unknown(api) -> None:
    result = runner.invoke(cli_main.think_app, ["session", "remove", "sess_missing"])
    assert result.exit_code == 1
    assert "not found" in result.output
