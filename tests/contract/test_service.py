"""Contract tests: /api/v1/service lifecycle routes (v1.7.1 kernel ``service`` tag).

The routes delegate to ``ServiceProcessManager``; tests inject a tmp-log-dir manager
and stub process control so no real server processes are spawned or killed.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from opal_think import lifecycle
from opal_think.lifecycle import ServiceProcessManager
from opal_think.routers import service as service_router


class _FakeProc:
    """Popen stand-in: running by default, or dead-on-arrival when `exit_code` is set."""

    def __init__(self, pid: int = 424242, exit_code: int | None = None) -> None:
        self.pid = pid
        self._exit_code = exit_code

    def poll(self) -> int | None:
        return self._exit_code


@pytest.fixture
def manager(config, tmp_path, monkeypatch) -> ServiceProcessManager:
    """Route ``_manager()`` to a tmp-dir manager so tests never touch ./logs."""
    mgr = ServiceProcessManager(config, log_dir=tmp_path / "logs")
    monkeypatch.setattr(service_router, "_manager", lambda _config: mgr)
    return mgr


def test_service_log_missing(client: TestClient, manager) -> None:
    response = client.get("/api/v1/service/log")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_service_log_tail(client: TestClient, manager) -> None:
    manager.log_file.parent.mkdir(parents=True)
    manager.log_file.write_text("one\ntwo\nthree\n", encoding="utf-8")
    body = client.get("/api/v1/service/log").json()
    assert body["lines"] == ["one", "two", "three"]  # default 10 returns all
    body = client.get("/api/v1/service/log", params={"lines": 2}).json()
    assert body["lines"] == ["two", "three"]  # oldest first
    assert body["log_file"].endswith("opal_think.log")


def test_service_log_lines_bounds(client: TestClient, manager) -> None:
    assert client.get("/api/v1/service/log", params={"lines": 0}).status_code == 422
    assert client.get("/api/v1/service/log", params={"lines": 1001}).status_code == 422
    assert client.get("/api/v1/service/log", params={"lines": "x"}).status_code == 422


def test_service_stop_not_running(client: TestClient, manager) -> None:
    body = client.post("/api/v1/service/stop").json()
    assert body == {"state": "stopped", "pid": None, "previous_pid": None, "log_file": str(manager.log_file)}


def test_service_stop_self(client: TestClient, manager, monkeypatch) -> None:
    """Tracked pid == this process: stop reports and schedules terminate_self."""
    manager.log_file.parent.mkdir(parents=True)
    manager.pid_file.write_text(str(os.getpid()), encoding="utf-8")
    calls = []
    monkeypatch.setattr(manager, "terminate_self", lambda: calls.append(True))
    body = client.post("/api/v1/service/stop").json()
    assert body["state"] == "stopped" and body["previous_pid"] == os.getpid()
    assert calls == [True]


def test_service_stop_tracked(client: TestClient, manager, monkeypatch) -> None:
    """Tracked foreign pid: stop_pid runs and the pidfile is removed."""
    manager.log_file.parent.mkdir(parents=True)
    manager.pid_file.write_text("999888", encoding="utf-8")
    monkeypatch.setattr(lifecycle, "pid_alive", lambda pid: True)
    stopped = []
    monkeypatch.setattr(lifecycle, "stop_pid", lambda pid: stopped.append(pid))
    body = client.post("/api/v1/service/stop").json()
    assert body["state"] == "stopped" and body["previous_pid"] == 999888
    assert stopped == [999888]
    assert not manager.pid_file.exists()


def test_service_start_spawns(client: TestClient, manager, monkeypatch) -> None:
    launches = []
    monkeypatch.setattr(manager, "launch", lambda append=False, wait_for_pids=(): launches.append(
        (append, list(wait_for_pids))) or _FakeProc())
    body = client.post("/api/v1/service/start", json={"append": True}).json()
    assert body["state"] == "running" and body["pid"] == 424242 and body["previous_pid"] is None
    assert launches == [(True, [])]


def test_service_start_replaces_tracked(client: TestClient, manager, monkeypatch) -> None:
    """A live foreign tracked pid is stopped first; its pid is reported as previous."""
    manager.log_file.parent.mkdir(parents=True)
    manager.pid_file.write_text("777111", encoding="utf-8")
    monkeypatch.setattr(lifecycle, "pid_alive", lambda pid: True)
    stopped = []
    monkeypatch.setattr(lifecycle, "stop_pid", lambda pid: stopped.append(pid))
    monkeypatch.setattr(manager, "launch", lambda append=False, wait_for_pids=(): _FakeProc())
    body = client.post("/api/v1/service/start").json()
    assert body["previous_pid"] == 777111
    assert stopped == [777111]


def test_service_start_self_restart(client: TestClient, manager, monkeypatch) -> None:
    """Tracked pid == self: the replacement child waits for this process to exit,
    and terminate_self is scheduled after the response."""
    manager.log_file.parent.mkdir(parents=True)
    manager.pid_file.write_text(str(os.getpid()), encoding="utf-8")
    launches = []
    monkeypatch.setattr(manager, "launch", lambda append=False, wait_for_pids=(): launches.append(
        list(wait_for_pids)) or _FakeProc())
    calls = []
    monkeypatch.setattr(manager, "terminate_self", lambda: calls.append(True))
    body = client.post("/api/v1/service/start").json()
    assert body["state"] == "running" and body["previous_pid"] == os.getpid()
    assert launches == [[os.getpid()]]
    assert calls == [True]


def test_service_start_early_death(client: TestClient, manager, monkeypatch) -> None:
    """A child that exits inside the early-death window surfaces 500, not 'running'."""
    manager.log_file.parent.mkdir(parents=True)
    manager.log_file.write_text("boom\n", encoding="utf-8")
    monkeypatch.setattr(manager, "launch", lambda append=False, wait_for_pids=(): _FakeProc(exit_code=2))
    response = client.post("/api/v1/service/start")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "operation_failed"
