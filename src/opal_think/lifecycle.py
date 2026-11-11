"""Managed server-process lifecycle for ``/api/v1/service/*`` and the CLI ``service`` group.

"The service's server process" is the pidfile-tracked detached ``python -m opal_think``
child: ``launch`` spawns it with output appended or truncated to ``logs/opal_think.log``
(per ``OpalServiceStartOptions.append``) and its real pid recorded in
``logs/opal_think.pid`` (written by the child itself via ``OPAL_THINK_PID_FILE``). When
the tracked process *is* the serving process, start/stop report and then terminate it
after the response is sent (``terminate_self``), and the replacement child is told via
``OPAL_THINK_WAIT_FOR_PID`` to wait for the parent to exit before binding.

Ported from opal-event's implementation (same kernel contract).
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from pathlib import Path

from opal_think.config import ThinkConfig
from opal_think.errors import OpalHTTPException

LOG_NAME = "opal_think.log"
PID_NAME = "opal_think.pid"

SELF_TERMINATE_DELAY = 0.4  # seconds for the 200 response to flush before self-termination
EARLY_DEATH_WINDOW = 1.0  # spawn failures (e.g. port conflict) surface within this window
WAIT_FOR_PIDS_TIMEOUT = 60.0


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        # os.kill(pid, 0) is unreliable on Windows; query the exit code with limited rights.
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # alive but not ours to signal
    return True


def stop_pid(pid: int) -> None:
    """Terminate `pid` (taskkill /T also takes children); escalate to kill after ~4s."""
    if os.name == "nt":
        with contextlib.suppress(OSError):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(40):
        if not pid_alive(pid):
            return
        time.sleep(0.1)
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)


def wait_for_pids(pids: Iterable[int | str], timeout: float = WAIT_FOR_PIDS_TIMEOUT) -> None:
    """Block until every listed pid exits. Raises TimeoutError on deadline."""
    alive = {int(p) for p in pids if str(p).strip()}
    deadline = time.monotonic() + timeout
    while alive:
        alive = {p for p in alive if pid_alive(p)}
        if not alive:
            return
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out waiting for pids {sorted(alive)} to exit")
        time.sleep(0.1)


class ServiceProcessManager:
    """Start/stop/tail the pidfile-tracked detached server.

    `log_dir` defaults to ``logs/`` under the current working directory — the same
    convention the CLI already used — and `config` is handed to the child so a
    managed restart keeps the same bind and configuration file."""

    def __init__(self, config: ThinkConfig | None = None, log_dir: Path | None = None):
        self.config = config
        self._log_dir = log_dir or Path("logs")

    @property
    def log_file(self) -> Path:
        return self._log_dir / LOG_NAME

    @property
    def pid_file(self) -> Path:
        return self._log_dir / PID_NAME

    def tracked_pid(self) -> int | None:
        """The managed server pid, or None when the pidfile is absent/stale/corrupt."""
        try:
            pid = int(self.pid_file.read_text().strip())
        except (OSError, ValueError):
            self.pid_file.unlink(missing_ok=True)
            return None
        if not pid_alive(pid):
            self.pid_file.unlink(missing_ok=True)
            return None
        return pid

    def launch(self, append: bool = False, wait_for_pids: Iterable[int] = ()) -> subprocess.Popen:
        """Spawn ``python -m opal_think`` detached, logging to the log file (truncated
        unless ``append``). The child records its own pid (``OPAL_THINK_PID_FILE``) and,
        when ``wait_for_pids`` is supplied, holds off binding until those pids exit —
        that is what lets ``service start`` restart the very process it runs in."""
        self._log_dir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "OPAL_THINK_PID_FILE": str(self.pid_file.resolve())}
        if self.config is not None:
            if self.config.config_path:
                env["OPAL_THINK_CONFIG"] = str(Path(self.config.config_path).resolve())
            env["OPAL_THINK_HOST"] = self.config.host
            env["OPAL_THINK_PORT"] = str(self.config.port)
        waits = list(wait_for_pids)
        if waits:
            env["OPAL_THINK_WAIT_FOR_PID"] = ",".join(str(p) for p in waits)
        log = self.log_file.open("ab" if append else "wb")
        kwargs: dict[str, object] = (
            {"start_new_session": True}
            if os.name != "nt"
            else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
        )
        try:
            return subprocess.Popen([sys.executable, "-m", "opal_think"], stdout=log, stderr=subprocess.STDOUT, env=env, **kwargs)
        finally:
            # The child holds its own duplicated handle; the parent's copy is redundant.
            log.close()

    def start(self, append: bool = False) -> tuple[dict, bool]:
        """Start (or restart) the managed server. Returns (status, terminate_self).

        A live tracked pid other than self is terminated first; when the tracked
        pid *is* self, the replacement child waits for this process to die before
        binding and the caller must run ``terminate_self`` after responding.
        """
        tracked = self.tracked_pid()
        restart_self = tracked is not None and tracked == os.getpid()
        previous: int | None = None
        waits: list[int] = []
        if restart_self:
            previous = os.getpid()
            waits = [previous]
        elif tracked is not None:
            stop_pid(tracked)
            self.pid_file.unlink(missing_ok=True)
            previous = tracked
        proc = self.launch(append=append, wait_for_pids=waits)
        # Catch immediate spawn failures (most commonly the port already being held by
        # an unmanaged server) rather than reporting a doomed child as running.
        deadline = time.monotonic() + EARLY_DEATH_WINDOW
        while time.monotonic() < deadline:
            code = proc.poll()
            if code is not None:
                tail = "\n".join((self.tail_log(5) or {}).get("lines", []))
                raise OpalHTTPException(
                    500, "operation_failed", f"managed server exited immediately (code {code}); log: {tail or self.log_file}"
                )
            time.sleep(0.05)
        status = {"state": "running", "pid": proc.pid, "previous_pid": previous, "log_file": str(self.log_file)}
        return status, restart_self

    def stop(self) -> tuple[dict, bool]:
        """Stop the managed server if running. Returns (status, terminate_self)."""
        tracked = self.tracked_pid()
        status = {"state": "stopped", "pid": None, "previous_pid": tracked, "log_file": str(self.log_file)}
        if tracked is None:
            return status, False
        if tracked == os.getpid():
            return status, True
        stop_pid(tracked)
        self.pid_file.unlink(missing_ok=True)
        return status, False

    def tail_log(self, lines: int) -> dict | None:
        """Trailing log lines (oldest first), or None when no log file exists."""
        try:
            text = self.log_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        return {"log_file": str(self.log_file), "lines": text.splitlines()[-lines:]}

    def terminate_self(self, delay: float = SELF_TERMINATE_DELAY) -> None:
        """Terminate this process shortly; run as a background task after the response.

        The tracked pidfile is removed first so a replacement child waiting on
        ``OPAL_THINK_WAIT_FOR_PID`` observes a clean exit ordering."""

        def _die() -> None:
            time.sleep(delay)
            try:
                if self.tracked_pid() == os.getpid():
                    self.pid_file.unlink(missing_ok=True)
            finally:
                # SIGINT maps to KeyboardInterrupt/CTRL+C on both platforms and drives
                # a graceful uvicorn shutdown; POSIX additionally honors SIGTERM.
                signal.raise_signal(signal.SIGTERM if os.name != "nt" else signal.SIGINT)

        threading.Thread(target=_die, daemon=True).start()
