"""CLI ``task`` commands against the real app via the MockTransport bridge."""

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
    test_client.app.state.settings.store.set_many(
        {"providers.ollama.enabled": False, "providers.fake.type": "fake", "providers.fake.enabled": True}
    )

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


def test_task_create_detach(api) -> None:
    result = runner.invoke(cli_main.think_app, ["task", "create", "hello", "--detach"])
    assert result.exit_code == 0, result.output
    assert result.output.strip().startswith("task_")


def test_task_create_awaits_completion(api) -> None:
    result = runner.invoke(cli_main.think_app, ["task", "create", "ping"])
    assert result.exit_code == 0, result.output
    assert "status=completed" in result.output
    assert "echo: ping" in result.output


def test_task_list_and_show(api) -> None:
    created = runner.invoke(cli_main.think_app, ["task", "create", "hello", "--detach"]).output.strip()
    result = runner.invoke(cli_main.think_app, ["task", "list"])
    assert result.exit_code == 0 and created in result.output
    result = runner.invoke(cli_main.think_app, ["--json", "task", "show", created])
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert body["id"] == created and body["status"] in {"queued", "running", "completed"}


def test_task_list_status_filter(api) -> None:
    result = runner.invoke(cli_main.think_app, ["--json", "task", "list", "--status", "failed"])
    assert result.exit_code == 0
    assert json.loads(result.output) == []


def test_task_show_unknown(api) -> None:
    result = runner.invoke(cli_main.think_app, ["task", "show", "task_missing"])
    assert result.exit_code == 1
    assert "not found" in result.output
