"""Unit tests for ServiceProcessManager internals (pidfile bookkeeping, log tail)."""

from __future__ import annotations

import os
from pathlib import Path

from opal_think.lifecycle import ServiceProcessManager, wait_for_pids


def _manager(tmp_path: Path) -> ServiceProcessManager:
    return ServiceProcessManager(log_dir=tmp_path / "logs")


def test_tracked_pid_absent_file(tmp_path) -> None:
    assert _manager(tmp_path).tracked_pid() is None


def test_tracked_pid_corrupt_file_removed(tmp_path) -> None:
    mgr = _manager(tmp_path)
    mgr.pid_file.parent.mkdir(parents=True)
    mgr.pid_file.write_text("not-a-pid", encoding="utf-8")
    assert mgr.tracked_pid() is None
    assert not mgr.pid_file.exists()


def test_tracked_pid_dead_process_removed(tmp_path, monkeypatch) -> None:
    import opal_think.lifecycle as lifecycle

    mgr = _manager(tmp_path)
    mgr.pid_file.parent.mkdir(parents=True)
    mgr.pid_file.write_text("31415926", encoding="utf-8")
    monkeypatch.setattr(lifecycle, "pid_alive", lambda pid: False)
    assert mgr.tracked_pid() is None
    assert not mgr.pid_file.exists()


def test_tracked_pid_live_process(tmp_path, monkeypatch) -> None:
    import opal_think.lifecycle as lifecycle

    mgr = _manager(tmp_path)
    mgr.pid_file.parent.mkdir(parents=True)
    mgr.pid_file.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr(lifecycle, "pid_alive", lambda pid: True)
    assert mgr.tracked_pid() == os.getpid()


def test_stop_with_stale_pidfile(tmp_path, monkeypatch) -> None:
    """A stale tracked pid is reported as previous but not signalled."""
    import opal_think.lifecycle as lifecycle

    mgr = _manager(tmp_path)
    mgr.pid_file.parent.mkdir(parents=True)
    mgr.pid_file.write_text("31415926", encoding="utf-8")
    monkeypatch.setattr(lifecycle, "pid_alive", lambda pid: False)
    stopped = []
    monkeypatch.setattr(lifecycle, "stop_pid", lambda pid: stopped.append(pid))
    status, terminate_self = mgr.stop()
    assert status["state"] == "stopped"
    # tracked_pid() already cleaned up the stale file, so nothing was stopped
    assert stopped == [] and terminate_self is False


def test_tail_log_missing_and_bounded(tmp_path) -> None:
    mgr = _manager(tmp_path)
    assert mgr.tail_log(5) is None
    mgr.log_file.parent.mkdir(parents=True)
    mgr.log_file.write_text("a\nb\nc\nd\n", encoding="utf-8")
    assert mgr.tail_log(2) == {"log_file": str(mgr.log_file), "lines": ["c", "d"]}


def test_wait_for_pids_empty_and_dead(monkeypatch) -> None:
    import opal_think.lifecycle as lifecycle

    wait_for_pids([])  # no-op
    wait_for_pids(["", "  "])  # blank entries ignored
    monkeypatch.setattr(lifecycle, "pid_alive", lambda pid: False)
    wait_for_pids([1, 2, 3], timeout=0.5)  # all already dead
