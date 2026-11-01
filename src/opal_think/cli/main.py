"""`opal think ...` command line. Every command calls the OPAL-THINK REST API over HTTP."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Any

import typer

from opal_think.cli.client import ApiCallError, ApiClient, api_url_from_env, token_from_env


@dataclass
class CliState:
    client: ApiClient
    json_output: bool
    context_headers: dict[str, str]


think_app = typer.Typer(
    help="OPAL Think: guarded reasoning service.", no_args_is_help=True, pretty_exceptions_enable=False
)
settings_app = typer.Typer(help="Portable service settings (strong-ETag guarded).", no_args_is_help=True)
think_app.add_typer(settings_app, name="settings")


@think_app.callback()
def think_callback(
    ctx: typer.Context,
    api_url: str = typer.Option(None, "--api-url", help="REST API base URL [env: OPAL_THINK_API_URL]"),
    token: str = typer.Option(None, "--token", help="Bearer token [env: OPAL_THINK_API_TOKEN]"),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON responses"),
    timeout: float = typer.Option(30.0, "--timeout", help="HTTP timeout in seconds"),
    opal_session_id: str = typer.Option(
        None, "--opal-session-id", help="X-Opal-Session-Id to send with every request [env: OPAL_SESSION_ID]"
    ),
    opal_transaction_id: str = typer.Option(
        None, "--opal-transaction-id", help="X-Opal-Transaction-Id to send with every request [env: OPAL_TRANSACTION_ID]"
    ),
    opal_causation_id: str = typer.Option(
        None, "--opal-causation-id", help="X-Opal-Causation-Id to send with every request [env: OPAL_CAUSATION_ID]"
    ),
) -> None:
    context_headers = {
        "X-Opal-Session-Id": opal_session_id or os.environ.get("OPAL_SESSION_ID"),
        "X-Opal-Transaction-Id": opal_transaction_id or os.environ.get("OPAL_TRANSACTION_ID"),
        "X-Opal-Causation-Id": opal_causation_id or os.environ.get("OPAL_CAUSATION_ID"),
    }
    context_headers = {k: v for k, v in context_headers.items() if v}
    ctx.obj = CliState(ApiClient(api_url or api_url_from_env(), token or token_from_env(), timeout), json_output, context_headers)


# -- helpers -----------------------------------------------------------------------------------


def _state(ctx: typer.Context) -> CliState:
    return ctx.find_root().obj if not isinstance(ctx.obj, CliState) else ctx.obj


def _call(ctx: typer.Context, method: str, path: str, **kwargs: Any):
    state = _state(ctx)
    if state.context_headers:
        kwargs["headers"] = {**state.context_headers, **(kwargs.get("headers") or {})}
    try:
        return state.client.request(method, path, **kwargs)
    except ApiCallError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


def _emit(ctx: typer.Context, data: Any) -> None:
    if _state(ctx).json_output or not isinstance(data, (dict, list)):
        typer.echo(json.dumps(data, indent=2, default=str) if isinstance(data, (dict, list)) else str(data))
        return
    _emit_human(data)


def _emit_human(data: Any) -> None:
    """Compact human-readable rendering; --json always gives the exact API payload."""
    if isinstance(data, dict) and "status" in data and "ready" in data:
        typer.echo(f"status: {data['status']}  ready: {data['ready']}")
        for key, value in data.items():
            if key not in {"status", "ready"}:
                typer.echo(f"{key}: {value}")
        return
    typer.echo(json.dumps(data, indent=2, default=str))


# -- health ------------------------------------------------------------------------------------


@think_app.command("health")
def health(
    ctx: typer.Context,
    live: bool = typer.Option(False, "--live", help="Liveness probe (process serving)"),
    ready: bool = typer.Option(False, "--ready", help="Readiness probe (usable model)"),
) -> None:
    """Service health: overall status, or --live / --ready probes."""
    path = "/api/v1/health/live" if live else "/api/v1/health/ready" if ready else "/api/v1/health/"
    _emit(ctx, _call(ctx, "GET", path).json())


# -- settings ----------------------------------------------------------------------------------


def _parse_value(raw: str) -> Any:
    """CLI values are native JSON; a bare word falls back to a plain string."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _setting_etag(ctx: typer.Context, key: str) -> str:
    return _call(ctx, "GET", f"/api/v1/settings/{key}").headers["ETag"]


@settings_app.command("list")
def settings_list(ctx: typer.Context) -> None:
    """List registered settings (portable values; secrets shown as references)."""
    data = _call(ctx, "GET", "/api/v1/settings/").json()
    if _state(ctx).json_output:
        _emit(ctx, data)
        return
    for item in data["items"]:
        suffix = "  (default)" if item["is_default"] else "  *"
        suffix += "  [restart pending]" if item["pending_restart"] else ""
        typer.echo(f"{item['key']} = {json.dumps(item['value'])}{suffix}")
    if data["pending_restart_fields"]:
        typer.echo(f"pending_restart_fields: {', '.join(data['pending_restart_fields'])}", err=True)


@settings_app.command("show")
def settings_show(ctx: typer.Context, key: str) -> None:
    """Show one registered setting."""
    _emit(ctx, _call(ctx, "GET", f"/api/v1/settings/{key}").json())


@settings_app.command("set")
def settings_set(
    ctx: typer.Context,
    key: str,
    value: str,
    if_match: str = typer.Option(None, "--if-match", help="ETag precondition (default: auto-fetched)"),
) -> None:
    """Set one setting to a native JSON value (auto-fetches the strong ETag)."""
    etag = if_match or _setting_etag(ctx, key)
    response = _call(ctx, "PUT", f"/api/v1/settings/{key}", json={"value": _parse_value(value)}, headers={"If-Match": etag})
    _emit(ctx, response.json())


@settings_app.command("remove")
def settings_remove(
    ctx: typer.Context,
    key: str,
    if_match: str = typer.Option(None, "--if-match", help="ETag precondition (default: auto-fetched)"),
) -> None:
    """Reset a setting to its registered default (removes the override)."""
    etag = if_match or _setting_etag(ctx, key)
    _call(ctx, "DELETE", f"/api/v1/settings/{key}", headers={"If-Match": etag})
    typer.echo(f"{key}: reset to default")


# -- `opal` root -------------------------------------------------------------------------------

app = typer.Typer(help="OPAL command line.", no_args_is_help=True, pretty_exceptions_enable=False)
app.add_typer(think_app, name="think")

try:  # Share the `opal` root with sibling OPAL CLIs when they are installed alongside this one.
    from opal_event.cli.main import event_app  # type: ignore[import-not-found]

    app.add_typer(event_app, name="event")
except Exception:  # noqa: BLE001
    pass

try:
    from opal_talk.cli.main import talk_app  # type: ignore[import-not-found]

    app.add_typer(talk_app, name="talk")
except Exception:  # noqa: BLE001
    pass

try:
    from opal_listener.cli.main import listen_app  # type: ignore[import-not-found]

    app.add_typer(listen_app, name="listen")
except Exception:  # noqa: BLE001
    pass


if __name__ == "__main__":
    sys.exit(think_app())
