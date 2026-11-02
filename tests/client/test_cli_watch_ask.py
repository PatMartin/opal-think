"""CLI ``task watch``, ``task cancel``, and ``ask`` over the MockTransport bridge."""

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
            content=request.read() if request.method in {"POST", "PUT", "PATCH"} else None,
        )

    def make_client(base_url, token=None, timeout=30.0, http=None):
        return ApiClient(base_url, token, timeout, http=httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(cli_main, "ApiClient", make_client)
    return test_client


def test_ask_streams_answer(api) -> None:
    result = runner.invoke(cli_main.think_app, ["ask", "ping"])
    assert result.exit_code == 0, result.output
    assert "echo: ping" in result.output


def test_ask_json(api) -> None:
    result = runner.invoke(cli_main.think_app, ["--json", "ask", "ping", "--no-stream"])
    assert result.exit_code == 0
    assert json.loads(result.output)["status"] == "completed"


def test_task_watch_terminal(api) -> None:
    task_id = runner.invoke(cli_main.think_app, ["task", "create", "hello", "--detach"]).output.strip()
    result = runner.invoke(cli_main.think_app, ["task", "watch", task_id])
    assert result.exit_code == 0, result.output
    assert "echo: hello" in result.output


def test_task_cancel(api) -> None:
    fake = next(a for a in api.app.state.providers.adapters() if a.provider_id == "fake")
    fake.delay = 5.0
    task_id = api.post("/api/v1/tasks/", json={"prompt": "slow"}).json()["id"]
    result = runner.invoke(cli_main.think_app, ["task", "cancel", task_id])
    assert result.exit_code == 0, result.output
    assert "cancel_requested=True" in result.output


def test_task_cancel_terminal_conflict(api) -> None:
    task_id = runner.invoke(cli_main.think_app, ["task", "create", "x", "--detach"]).output.strip()
    import time

    deadline = time.monotonic() + 5
    while api.get(f"/api/v1/tasks/{task_id}").json()["status"] not in {"completed", "failed"}:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    result = runner.invoke(cli_main.think_app, ["task", "cancel", task_id])
    assert result.exit_code == 1
    assert "terminal" in result.output
