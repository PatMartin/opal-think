"""``opal-think-svc`` entry point: load config, enforce loopback/auth policy, run uvicorn."""

from __future__ import annotations

import sys

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

    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port)


def main() -> int:
    svc_app()
    return 0


if __name__ == "__main__":
    sys.exit(main())
