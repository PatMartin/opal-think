"""CLI ``profile`` / ``config setting`` / ``config save|load|validate`` over the mock bridge."""

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


def test_profile_lifecycle(api, tmp_path) -> None:
    result = runner.invoke(cli_main.think_app, ["profile", "add", "--name", "studio"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli_main.think_app, ["profile", "list"])
    assert "studio" in result.output
    result = runner.invoke(cli_main.think_app, ["profile", "activate", "1"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli_main.think_app, ["profile", "remove", "1"])
    assert result.exit_code != 0  # active profile cannot be deleted
    result = runner.invoke(cli_main.think_app, ["profile", "update", "1", "--no-is-active"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli_main.think_app, ["profile", "remove", "1"])
    assert result.exit_code == 0, result.output
    assert api.get("/api/v1/profile/").json() == []


def test_profile_add_in_file(api, tmp_path) -> None:
    """``profile add --in`` binds a whole portable profile document."""
    doc = tmp_path / "p.json"
    doc.write_text(json.dumps({"name": "studio", "modules": {"opal-say": {"voice_id": "en-gb"}}}))
    result = runner.invoke(cli_main.think_app, ["profile", "add", "--in", str(doc)])
    assert result.exit_code == 0, result.output
    profile = api.get("/api/v1/profile/").json()[0]
    assert profile["id"] == "1" and profile["modules"] == {"opal-say": {"voice_id": "en-gb"}}
    result = runner.invoke(cli_main.think_app, ["profile", "show", "1"])
    assert result.exit_code == 0


def test_setting_set(api) -> None:
    for key, value in (("task.max_concurrent", "6"), ("task.retention_days", "10")):
        result = runner.invoke(cli_main.think_app, ["config", "setting", "set", key, value])
        assert result.exit_code == 0, result.output
    assert api.get("/api/v1/setting/task.max_concurrent").json()["value"] == 6
    assert api.get("/api/v1/setting/task.retention_days").json()["value"] == 10


def test_config_save_and_validate(api, tmp_path) -> None:
    snap_file = tmp_path / "snap.json"
    result = runner.invoke(cli_main.think_app, ["config", "save", "--out", str(snap_file)])
    assert result.exit_code == 0, result.output
    snap = __import__("json").loads(snap_file.read_text())
    assert snap["module"] == "opal-think" and snap["format_version"] == "1.0.0"
    result = runner.invoke(cli_main.think_app, ["config", "validate", "--in", str(snap_file)])
    assert result.exit_code == 0, result.output
    bad = tmp_path / "bad.json"
    bad.write_text(__import__("json").dump if False else __import__("json").dumps({**snap, "settings": {"bad.k": 1}}))
    result = runner.invoke(cli_main.think_app, ["config", "validate", "--in", str(bad)])
    assert result.exit_code != 0


def test_config_managed_round_trip(api) -> None:
    result = runner.invoke(cli_main.think_app, ["config", "save"])
    assert result.exit_code == 0, result.output
    assert api.get("/api/v1/setting/task.retention_days").json()["is_default"] is True
    runner.invoke(cli_main.think_app, ["config", "setting", "set", "task.retention_days", "10"])
    result = runner.invoke(cli_main.think_app, ["config", "load"])
    assert result.exit_code == 0, result.output
    assert api.get("/api/v1/setting/task.retention_days").json()["is_default"] is True
