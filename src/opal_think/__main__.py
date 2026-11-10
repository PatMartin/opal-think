"""``opal-think-svc`` entry point: load config, enforce loopback/auth policy, run uvicorn."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer
import uvicorn

from opal_think.config import load_config

svc_app = typer.Typer(help="Run the OPAL-THINK REST service.", pretty_exceptions_enable=False)


@svc_app.callback(invoke_without_command=True)
def run(
    config: str = typer.Option(None, "--config", help="YAML config file [env: OPAL_THINK_CONFIG]"),
    host: str = typer.Option(None, "--host", help="Bind host [env: OPAL_THINK_HOST]"),
    port: int = typer.Option(None, "--port", help="Bind port [env: OPAL_THINK_PORT]"),
) -> None:
    cfg = load_config(config)
    if host:
        cfg.host = host
    if port:
        cfg.port = port
    try:
        cfg.validate()
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc

    from opal_think.app import create_app

    # Record the real server pid for `opal think service stop` — the spawning CLI's
    # Popen pid can differ (venv python.exe is a shim that re-execs the interpreter).
    pid_file = Path("logs") / "opal_think.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    try:
        uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port)
    finally:
        pid_file.unlink(missing_ok=True)


def main() -> int:
    svc_app()
    return 0


if __name__ == "__main__":
    sys.exit(main())
