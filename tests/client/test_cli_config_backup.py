"""CLI ``config profile`` / ``backup`` / ``config setting update`` over the mock bridge."""

from __future__ import annotations

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


def test_profile_lifecycle(api) -> None:
    result = runner.invoke(
        cli_main.think_app,
        ["config", "profile", "add", "studio", "--settings", '{"task.default_max_steps": 4}'],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli_main.think_app, ["config", "profile", "list"])
    assert "studio" in result.output
    result = runner.invoke(cli_main.think_app, ["config", "profile", "activate", "1"])
    assert result.exit_code == 0, result.output
    assert api.get("/api/v1/setting/task.default_max_steps").json()["value"] == 4
    result = runner.invoke(cli_main.think_app, ["config", "profile", "remove", "1"])
    assert result.exit_code != 0  # active profile cannot be deleted
    result = runner.invoke(cli_main.think_app, ["config", "profile", "update", "1", "--inactive"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli_main.think_app, ["config", "profile", "remove", "1"])
    assert result.exit_code == 0, result.output
    assert api.get("/api/v1/profile/").json() == []


def test_setting_update(api) -> None:
    result = runner.invoke(
        cli_main.think_app,
        ["config", "setting", "update", '{"task.max_concurrent": 6, "task.retention_days": 10}'],
    )
    assert result.exit_code == 0, result.output
    assert api.get("/api/v1/setting/task.max_concurrent").json()["value"] == 6
    assert api.get("/api/v1/setting/task.retention_days").json()["value"] == 10
    result = runner.invoke(cli_main.think_app, ["config", "setting", "update", "not json"])
    assert result.exit_code != 0


def test_backup_export_and_validate(api) -> None:
    result = runner.invoke(cli_main.think_app, ["backup", "export"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        cli_main.think_app, ["backup", "validate", "--json", '{"format_version": "1.0.0", "settings": {}}']
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        cli_main.think_app, ["backup", "validate", "--json", '{"format_version": "1.0.0", "settings": {"bad.k": 1}}']
    )
    assert result.exit_code != 0


def test_backup_archive_cycle(api) -> None:
    assert runner.invoke(cli_main.think_app, ["backup", "list"]).exit_code == 0
    result = runner.invoke(cli_main.think_app, ["backup", "create"])
    assert result.exit_code == 0, result.output
    filename = api.get("/api/v1/backup/list").json()[0]["filename"]
    assert runner.invoke(cli_main.think_app, ["backup", "restore", filename]).exit_code == 0
    assert runner.invoke(cli_main.think_app, ["backup", "remove", filename]).exit_code == 0
    assert api.get("/api/v1/backup/list").json() == []
