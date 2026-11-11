"""`opal think ...` command line. Every command calls the OPAL-THINK REST API over HTTP."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import typer
from typer.main import get_command

from opal_think.cli.client import ApiCallError, ApiClient, api_url_from_env, token_from_env
from opal_think.config import load_config


@dataclass
class CliState:
    client: ApiClient
    json_output: bool
    context_headers: dict[str, str]


think_app = typer.Typer(
    help="OPAL Think: guarded reasoning service.", no_args_is_help=True, pretty_exceptions_enable=False
)
sessions_app = typer.Typer(help="Optional conversation context containers.", no_args_is_help=True)
models_app = typer.Typer(help="Discovered model catalog (read-only).", no_args_is_help=True)
tasks_app = typer.Typer(
    help="Task lifecycle: create submits work, list/show inspect it, watch streams it to a terminal state, "
    "cancel requests cooperative cancellation. `ask` and `chat` are shortcuts over this same lifecycle.",
    no_args_is_help=True)
info_app = typer.Typer(help="Service identity, version and health probes.", no_args_is_help=True)
backup_app = typer.Typer(help="Portable configuration export, archives and restore.", no_args_is_help=True)
service_app = typer.Typer(help="Managed opal-think-svc process lifecycle.", no_args_is_help=True)
# Canonical x-opal-cli bindings (v1.7.1): `config` parents the portable-configuration
# groups — setting, persona, profile.
config_app = typer.Typer(help="Portable configuration resources.", no_args_is_help=True)
config_setting_app = typer.Typer(help="Portable service settings (strong-ETag guarded).", no_args_is_help=True)
config_persona_app = typer.Typer(help="Named assistant personas (shared OPAL identities).", no_args_is_help=True)
config_profile_app = typer.Typer(help="Named presets of portable operating defaults.", no_args_is_help=True)
config_app.add_typer(config_setting_app, name="setting")
config_app.add_typer(config_persona_app, name="persona")
config_app.add_typer(config_profile_app, name="profile")
think_app.add_typer(sessions_app, name="session")
think_app.add_typer(models_app, name="model")
think_app.add_typer(tasks_app, name="task")
think_app.add_typer(info_app, name="info")
think_app.add_typer(config_app, name="config")
think_app.add_typer(backup_app, name="backup")
think_app.add_typer(service_app, name="service")

_TERMINAL = {"completed", "failed", "cancelled", "timed_out"}


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


# -- info (service identity, version, health, liveness) ------------------------------------------


@info_app.command("health")
def health(ctx: typer.Context) -> None:
    """Service health: overall status, model availability and EVENT state."""
    _emit(ctx, _call(ctx, "GET", "/api/v1/health/").json())


@info_app.command("live")
def live(ctx: typer.Context) -> None:
    """Liveness probe (process serving)."""
    _emit(ctx, _call(ctx, "GET", "/api/v1/health/live").json())


@info_app.command("ready")
def ready(ctx: typer.Context) -> None:
    """Readiness probe (a usable model is configured and available)."""
    _emit(ctx, _call(ctx, "GET", "/api/v1/health/ready").json())


@info_app.command("version")
def version(ctx: typer.Context) -> None:
    """Service identity: implementation name, package and contract versions."""
    _emit(ctx, _call(ctx, "GET", "/api/v1/info/").json())


@info_app.command("ping")
def ping(ctx: typer.Context) -> None:
    """Lightweight liveness probe for process supervisors."""
    _emit(ctx, _call(ctx, "GET", "/api/v1/info/ping").json())


@think_app.command("gui")
def gui(ctx: typer.Context) -> None:
    """Open the SPA in a browser (served by the running service at its base URL)."""
    import webbrowser

    state = _state(ctx)
    try:
        state.client.get("/api/v1/info/ping", timeout=2.0)
    except ApiCallError:
        typer.echo(
            f"service unreachable at {state.client.base_url}; start it with `opal think service start`",
            err=True,
        )
        raise typer.Exit(1) from None
    webbrowser.open(state.client.base_url)
    typer.echo(f"opened {state.client.base_url} in your browser")


# -- settings ----------------------------------------------------------------------------------


def _parse_value(raw: str) -> Any:
    """CLI values are native JSON; a bare word falls back to a plain string."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _setting_etag(ctx: typer.Context, key: str) -> str:
    return _call(ctx, "GET", f"/api/v1/setting/{key}").headers["ETag"]


@config_setting_app.command("list")
def settings_list(ctx: typer.Context) -> None:
    """List registered settings (portable values; secrets shown as references)."""
    data = _call(ctx, "GET", "/api/v1/setting/").json()
    if _state(ctx).json_output:
        _emit(ctx, data)
        return
    # The collection is a flat OpalSettingsMap; per-key GETs supply default/restart flags.
    for key in sorted(k for k in data if k != "pending_restart_fields"):
        try:
            item = _call(ctx, "GET", f"/api/v1/setting/{key}").json()
        except typer.Exit:
            item = {"key": key, "value": data[key], "is_default": True, "pending_restart": False}
        suffix = "  (default)" if item.get("is_default", True) else "  *"
        suffix += "  [restart pending]" if item.get("pending_restart") else ""
        typer.echo(f"{key} = {json.dumps(item.get('value', data[key]))}{suffix}")
    if data["pending_restart_fields"]:
        typer.echo(f"pending_restart_fields: {', '.join(data['pending_restart_fields'])}", err=True)


@config_setting_app.command("show")
def settings_show(ctx: typer.Context, key: str) -> None:
    """Show one registered setting."""
    _emit(ctx, _call(ctx, "GET", f"/api/v1/setting/{key}").json())


@config_setting_app.command("set")
def settings_set(
    ctx: typer.Context,
    key: str,
    value: str,
    if_match: str = typer.Option(None, "--if-match", help="ETag precondition (default: auto-fetched)"),
) -> None:
    """Set one setting to a native JSON value (auto-fetches the strong ETag)."""
    etag = if_match or _setting_etag(ctx, key)
    response = _call(ctx, "PUT", f"/api/v1/setting/{key}", json={"value": _parse_value(value)}, headers={"If-Match": etag})
    _emit(ctx, response.json())


@config_setting_app.command("remove")
def settings_remove(
    ctx: typer.Context,
    key: str,
    if_match: str = typer.Option(None, "--if-match", help="ETag precondition (default: auto-fetched)"),
) -> None:
    """Reset a setting to its registered default (removes the override)."""
    etag = if_match or _setting_etag(ctx, key)
    _call(ctx, "DELETE", f"/api/v1/setting/{key}", headers={"If-Match": etag})
    typer.echo(f"{key}: reset to default")


@config_setting_app.command("update")
def settings_update(
    ctx: typer.Context,
    patch: str = typer.Argument(..., help="JSON object of setting key -> value pairs to apply atomically"),
    if_match: str = typer.Option(None, "--if-match", help="ETag precondition (default: auto-fetched)"),
) -> None:
    """Atomically update several settings (PATCH the collection; auto-fetches the config ETag)."""
    try:
        body = json.loads(patch)
    except ValueError as exc:
        typer.echo(f"error: patch must be a JSON object: {exc}", err=True)
        raise typer.Exit(1) from exc
    if not isinstance(body, dict):
        typer.echo("error: patch must be a JSON object", err=True)
        raise typer.Exit(1)
    etag = if_match or _call(ctx, "GET", "/api/v1/setting/").headers["ETag"]
    _emit(ctx, _call(ctx, "PATCH", "/api/v1/setting/", json=body, headers={"If-Match": etag}).json())


# -- config profile (named presets of portable defaults) ----------------------------------------------


@config_profile_app.command("list")
def profile_list(ctx: typer.Context) -> None:
    """List profiles (complete bounded catalog; no pagination)."""
    data = _call(ctx, "GET", "/api/v1/profile/").json()
    if _state(ctx).json_output:
        _emit(ctx, data)
        return
    for p in data:
        active = " *active*" if p.get("is_active") else ""
        settings = json.dumps(p["settings"]) if p.get("settings") else "-"
        typer.echo(f"{p['id']}  {p['name']}{active}  settings={settings}")


def _profile_etag(ctx: typer.Context, profile_id: int) -> str:
    return _call(ctx, "GET", f"/api/v1/profile/{profile_id}").headers["ETag"]


@config_profile_app.command("add")
def profile_add(
    ctx: typer.Context,
    name: str,
    settings: str = typer.Option(None, "--settings", help="JSON preset of portable setting key -> value pairs"),
    description: str = typer.Option(None, "--description"),
    profile_id: int = typer.Option(None, "--id", help="Client-supplied numeric profile id"),
    active: bool = typer.Option(False, "--active", help="Make this the active profile (applies the preset)"),
    if_match: str = typer.Option(None, "--if-match", help="ETag precondition (default: auto-fetched)"),
) -> None:
    """Create a named preset of portable setting defaults (auto-fetches the config ETag)."""
    body: dict[str, Any] = {"name": name, "is_active": active}
    if profile_id is not None:
        body["id"] = profile_id
    if description is not None:
        body["description"] = description
    if settings is not None:
        try:
            body["settings"] = json.loads(settings)
        except ValueError as exc:
            typer.echo(f"error: --settings must be a JSON object: {exc}", err=True)
            raise typer.Exit(1) from exc
        if not isinstance(body["settings"], dict):
            typer.echo("error: --settings must be a JSON object", err=True)
            raise typer.Exit(1)
    etag = if_match or _config_etag(ctx)
    _emit(ctx, _call(ctx, "POST", "/api/v1/profile/", json=body, headers={"If-Match": etag}).json())


@config_profile_app.command("show")
def profile_show(ctx: typer.Context, profile_id: int) -> None:
    """Show one profile, including all service compartments."""
    _emit(ctx, _call(ctx, "GET", f"/api/v1/profile/{profile_id}").json())


@config_profile_app.command("update")
def profile_update(
    ctx: typer.Context,
    profile_id: int,
    name: str = typer.Option(None, "--name"),
    description: str = typer.Option(None, "--description"),
    settings: str = typer.Option(None, "--settings", help="JSON preset replacing the settings compartment"),
    active: bool = typer.Option(None, "--active/--inactive", help="Activate (applies preset) / deactivate"),
    if_match: str = typer.Option(None, "--if-match", help="ETag precondition (default: auto-fetched)"),
) -> None:
    """Partially update a profile (auto-fetches the config ETag)."""
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if active is not None:
        body["is_active"] = active
    if settings is not None:
        try:
            body["settings"] = json.loads(settings)
        except ValueError as exc:
            typer.echo(f"error: --settings must be a JSON object: {exc}", err=True)
            raise typer.Exit(1) from exc
    if not body:
        typer.echo("error: nothing to update (pass at least one field option)", err=True)
        raise typer.Exit(1)
    etag = if_match or _profile_etag(ctx, profile_id)
    _emit(ctx, _call(ctx, "PATCH", f"/api/v1/profile/{profile_id}", json=body, headers={"If-Match": etag}).json())


@config_profile_app.command("remove")
def profile_remove(
    ctx: typer.Context,
    profile_id: int,
    if_match: str = typer.Option(None, "--if-match", help="ETag precondition (default: auto-fetched)"),
) -> None:
    """Remove a profile (409 while it is the active profile; auto-fetches the config ETag)."""
    etag = if_match or _profile_etag(ctx, profile_id)
    _call(ctx, "DELETE", f"/api/v1/profile/{profile_id}", headers={"If-Match": etag})
    typer.echo(f"{profile_id}: removed")


@config_profile_app.command("activate")
def profile_activate(
    ctx: typer.Context,
    profile_id: int,
    if_match: str = typer.Option(None, "--if-match", help="ETag precondition (default: auto-fetched)"),
) -> None:
    """Make this profile the only active one and apply its settings preset."""
    etag = if_match or _profile_etag(ctx, profile_id)
    _emit(ctx, _call(ctx, "POST", f"/api/v1/profile/{profile_id}/activate", headers={"If-Match": etag}).json())


# -- session -----------------------------------------------------------------------------------


@sessions_app.command("add")
def session_add(
    ctx: typer.Context,
    title: str = typer.Option(None, "--title", help="Optional human-readable label"),
) -> None:
    """Create a session (a durable conversation context container)."""
    response = _call(ctx, "POST", "/api/v1/session/", json={"title": title})
    data = response.json()
    if _state(ctx).json_output:
        _emit(ctx, data)
    else:
        typer.echo(f"session_id: {data['id']}")
        if data["title"]:
            typer.echo(f"title: {data['title']}")


@sessions_app.command("list")
def session_list(
    ctx: typer.Context,
    limit: int = typer.Option(50, "--limit", min=1, max=200),
) -> None:
    """List sessions, newest updated first."""
    params: dict[str, Any] = {"limit": limit}
    data = _call(ctx, "GET", "/api/v1/session/", params=params).json()
    if _state(ctx).json_output:
        _emit(ctx, data)
        return
    for item in data:
        typer.echo(f"{item['id']}  {item['title'] or '-'}  tasks={item['task_count']}  updated={item['updated_at']}")


@sessions_app.command("show")
def session_show(ctx: typer.Context, session_id: str) -> None:
    """Show one session's metadata."""
    _emit(ctx, _call(ctx, "GET", f"/api/v1/session/{session_id}").json())


@sessions_app.command("remove")
def session_remove(ctx: typer.Context, session_id: str) -> None:
    """Remove a session and its retained task records (409 while tasks are active)."""
    _call(ctx, "DELETE", f"/api/v1/session/{session_id}")
    typer.echo(f"{session_id}: removed")


# -- model -------------------------------------------------------------------------------------


@models_app.command("list")
def model_list(ctx: typer.Context) -> None:
    """List discovered models across configured providers."""
    data = _call(ctx, "GET", "/api/v1/model/").json()
    if _state(ctx).json_output:
        _emit(ctx, data)
        return
    if not data:
        typer.echo("no models discovered (all configured providers unreachable or none enabled)", err=True)
        return
    for m in data:
        caps = ",".join(k for k, v in m["capabilities"].items() if v)
        ctx_size = m["context_window"] or "-"
        avail = "yes" if m["available"] else "no"
        typer.echo(f"{m['id']}  provider={m['provider']}  available={avail}  ctx={ctx_size}  caps={caps}")


@models_app.command("show")
def model_show(ctx: typer.Context, model_id: str) -> None:
    """Show one model's capabilities (opaque ID, e.g. ollama:llama3.2)."""
    _emit(ctx, _call(ctx, "GET", f"/api/v1/model/{model_id}").json())


# -- task --------------------------------------------------------------------------------------


def _print_task(data: dict) -> None:
    typer.echo(f"task {data['id']}  status={data['status']}  model={data['model_id']}  steps={data['steps_completed']}")
    if data.get("answer"):
        typer.echo(data["answer"])
    if data.get("error"):
        typer.echo(f"error: {data['error']['code']}: {data['error']['message']}", err=True)


def _await_task(ctx: typer.Context, task_id: str, timeout: float = 300.0) -> dict:
    """Poll show_task until the task reaches a terminal state (watch lands with SSE)."""
    deadline = time.monotonic() + timeout
    while True:
        data = _call(ctx, "GET", f"/api/v1/task/{task_id}").json()
        if data["status"] in _TERMINAL or time.monotonic() > deadline:
            return data
        time.sleep(0.5)


@tasks_app.command("create")
def task_create(
    ctx: typer.Context,
    prompt: str,
    model: str = typer.Option(None, "--model", help="Model id (e.g. ollama:llama3.2)"),
    session_id: str = typer.Option(None, "--session-id", help="Existing session to serialize into"),
    max_steps: int = typer.Option(None, "--max-steps", min=1, max=100),
    timeout: int = typer.Option(None, "--timeout", help="Task deadline seconds", min=1, max=3600),
    detach: bool = typer.Option(False, "--detach", help="Return the task id without awaiting"),
) -> None:
    """Submit one unit of work; prints the task id (--detach) or waits for completion.

    Without --session-id the task is stateless; pass --session-id to serialize
    the task into an existing session so turns share context. Follow with
    `task watch` to stream progress or `task cancel` to request cancellation.
    """
    body: dict[str, Any] = {"prompt": prompt}
    if model:
        body["model_id"] = model
    if session_id:
        body["session_id"] = session_id
    if max_steps:
        body["max_steps"] = max_steps
    if timeout:
        body["timeout_seconds"] = timeout
    data = _call(ctx, "POST", "/api/v1/task/", json=body).json()
    if detach:
        if _state(ctx).json_output:
            _emit(ctx, data)
        else:
            typer.echo(data["id"])
        return
    data = _await_task(ctx, data["id"], timeout=float(timeout or 300))
    if _state(ctx).json_output:
        _emit(ctx, data)
    else:
        _print_task(data)
    if data["status"] not in {"completed"}:
        raise typer.Exit(2)


@tasks_app.command("list")
def task_list(
    ctx: typer.Context,
    status: str = typer.Option(None, "--status", help="queued|running|completed|failed|cancelled|timed_out"),
    session_id: str = typer.Option(None, "--session-id"),
    limit: int = typer.Option(50, "--limit", min=1, max=200),
) -> None:
    """List retained tasks, newest first."""
    params: dict[str, Any] = {"limit": limit}
    if status:
        params["status"] = status
    if session_id:
        params["session_id"] = session_id
    data = _call(ctx, "GET", "/api/v1/task/", params=params).json()
    if _state(ctx).json_output:
        _emit(ctx, data)
        return
    for t in data:
        typer.echo(f"{t['id']}  {t['status']:<10}  {t['model_id']}  {t['prompt'][:60]}")


@tasks_app.command("show")
def task_show(ctx: typer.Context, task_id: str) -> None:
    """Authoritative snapshot: status, answer, bounded steps."""
    _emit(ctx, _call(ctx, "GET", f"/api/v1/task/{task_id}").json())


def _watch_stream(ctx: typer.Context, task_id: str) -> dict | None:
    """Follow the task SSE stream to a terminal event; returns terminal frame data."""
    state = _state(ctx)
    json_mode = state.json_output
    with state.client.stream(f"/api/v1/task/{task_id}/stream") as response:
        if response.status_code >= 400:
            raise ApiCallError(response.status_code, response.text)
        saw_output = False
        for line in response.iter_lines():
            if line.startswith("data:"):
                frame = json.loads(line[5:].strip())
                if json_mode:
                    typer.echo(json.dumps(frame, default=str))
                else:
                    data = frame["data"]
                    if frame["type"] == "task.output":
                        saw_output = True
                        typer.echo(data.get("text", ""), nl=False)
                    elif frame["type"] == "task.action":
                        typer.echo(f"\n[action: {data.get('action_id')}]")
                    elif frame["type"] == "task.progress":
                        pass
                    elif frame["type"] in _TERMINAL_EVENTS:
                        # late connects get a terminal snapshot only; print its stored answer
                        if not saw_output and data.get("answer"):
                            typer.echo(data["answer"])
                        typer.echo("")
                if frame["type"] in _TERMINAL_EVENTS:
                    return frame["data"]
    return None


_TERMINAL_EVENTS = {f"task.{s}" for s in _TERMINAL}


@tasks_app.command("watch")
def task_watch(ctx: typer.Context, task_id: str) -> None:
    """Follow a task's live stream until its terminal state, then show the snapshot."""
    _watch_stream(ctx, task_id)
    data = _call(ctx, "GET", f"/api/v1/task/{task_id}").json()
    if _state(ctx).json_output:
        _emit(ctx, data)
    elif not data.get("answer"):
        typer.echo(f"status={data['status']}" + (f"  error={data['error']['message']}" if data.get("error") else ""))
    if data["status"] != "completed":
        raise typer.Exit(2)


@tasks_app.command("cancel")
def task_cancel(ctx: typer.Context, task_id: str) -> None:
    """Request cooperative cancellation (terminal tasks return 409)."""
    data = _call(ctx, "POST", f"/api/v1/task/{task_id}/cancel").json()
    if _state(ctx).json_output:
        _emit(ctx, data)
    else:
        typer.echo(f"{data['id']}  status={data['status']}  cancel_requested={data['cancel_requested']}")


# -- persona -----------------------------------------------------------------------------------


def _config_etag(ctx: typer.Context) -> str:
    """Shared portable-configuration revision; persona list carries it as ETag."""
    return _call(ctx, "GET", "/api/v1/persona/").headers["ETag"]


def _persona_etag(ctx: typer.Context, persona_id: str) -> str:
    return _call(ctx, "GET", f"/api/v1/persona/{persona_id}").headers["ETag"]


@config_persona_app.command("list")
def persona_list(
    ctx: typer.Context,
    enabled: bool = typer.Option(None, "--enabled/--disabled", help="Filter by enabled state"),
) -> None:
    """List registered personas (complete bounded catalog; no pagination)."""
    params = {} if enabled is None else {"enabled": enabled}
    data = _call(ctx, "GET", "/api/v1/persona/", params=params).json()
    if _state(ctx).json_output:
        _emit(ctx, data)
        return
    for p in data:
        voice = p["voice_id"] or "-"
        typer.echo(f"{p['persona_id']}  {p['name']}  enabled={p['enabled']}  voice={voice}")


@config_persona_app.command("add")
def persona_create(
    ctx: typer.Context,
    persona_id: str,
    name: str = typer.Option(..., "--name", help="Human-readable persona name"),
    description: str = typer.Option(None, "--description", help="Optional character notes"),
    voice_id: str = typer.Option(None, "--voice-id", help="OPAL Say voice binding"),
    disabled: bool = typer.Option(False, "--disabled", help="Create disabled (default: enabled)"),
    if_match: str = typer.Option(None, "--if-match", help="ETag precondition (default: auto-fetched)"),
) -> None:
    """Register a persona (auto-fetches the configuration ETag)."""
    body: dict[str, Any] = {"persona_id": persona_id, "name": name, "enabled": not disabled}
    if description is not None:
        body["description"] = description
    if voice_id is not None:
        body["voice_id"] = voice_id
    etag = if_match or _config_etag(ctx)
    data = _call(ctx, "POST", "/api/v1/persona/", json=body, headers={"If-Match": etag}).json()
    if _state(ctx).json_output:
        _emit(ctx, data)
    else:
        typer.echo(f"persona_id: {data['persona_id']}")


@config_persona_app.command("show")
def persona_show(ctx: typer.Context, persona_id: str) -> None:
    """Show one registered persona."""
    _emit(ctx, _call(ctx, "GET", f"/api/v1/persona/{persona_id}").json())


@config_persona_app.command("update")
def persona_update(
    ctx: typer.Context,
    persona_id: str,
    name: str = typer.Option(None, "--name", help="New human-readable name"),
    description: str = typer.Option(None, "--description", help="New description"),
    voice_id: str = typer.Option(None, "--voice-id", help="Rebind to a different OPAL Say voice"),
    clear_description: bool = typer.Option(False, "--clear-description", help="Set description to null"),
    clear_voice: bool = typer.Option(False, "--clear-voice", help="Remove the voice binding"),
    enabled: bool = typer.Option(None, "--enabled/--disabled", help="Set enabled state"),
    if_match: str = typer.Option(None, "--if-match", help="ETag precondition (default: auto-fetched)"),
) -> None:
    """Partially update a persona (auto-fetches the configuration ETag)."""
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if clear_description:
        body["description"] = None
    if voice_id is not None:
        body["voice_id"] = voice_id
    if clear_voice:
        body["voice_id"] = None
    if enabled is not None:
        body["enabled"] = enabled
    if not body:
        typer.echo("error: nothing to update (pass at least one field option)", err=True)
        raise typer.Exit(1)
    etag = if_match or _persona_etag(ctx, persona_id)
    _emit(ctx, _call(ctx, "PATCH", f"/api/v1/persona/{persona_id}", json=body, headers={"If-Match": etag}).json())


@config_persona_app.command("remove")
def persona_remove(
    ctx: typer.Context,
    persona_id: str,
    if_match: str = typer.Option(None, "--if-match", help="ETag precondition (default: auto-fetched)"),
) -> None:
    """Remove a persona registration (auto-fetches the configuration ETag)."""
    etag = if_match or _persona_etag(ctx, persona_id)
    _call(ctx, "DELETE", f"/api/v1/persona/{persona_id}", headers={"If-Match": etag})
    typer.echo(f"{persona_id}: removed")


@config_persona_app.command("validate")
def persona_validate(ctx: typer.Context, persona_id: str) -> None:
    """Check that a persona is registered, enabled, and its bindings resolve."""
    data = _call(ctx, "POST", f"/api/v1/persona/{persona_id}/validate").json()
    _emit(ctx, data)
    if not data["valid"]:
        raise typer.Exit(1)


@think_app.command("ask")
def ask(
    ctx: typer.Context,
    prompt: str,
    model: str = typer.Option(None, "--model", help="Model id"),
    session_id: str = typer.Option(None, "--session-id", help="Existing session id"),
    stream: bool = typer.Option(True, "--stream/--no-stream", help="Stream output live"),
) -> None:
    """Stateless one-shot: create one task, stream it, print the answer.

    Shortcut for `task create` + `task watch`. No session is created unless
    --session-id names an existing session to serialize the turn into.
    """
    body: dict[str, Any] = {"prompt": prompt}
    if model:
        body["model_id"] = model
    if session_id:
        body["session_id"] = session_id
    data = _call(ctx, "POST", "/api/v1/task/", json=body).json()
    if stream:
        _watch_stream(ctx, data["id"])
    data = _await_task(ctx, data["id"]) if not stream else _call(ctx, "GET", f"/api/v1/task/{data['id']}").json()
    if _state(ctx).json_output:
        _emit(ctx, data)
    elif not stream:
        _print_task(data)
    if data["status"] != "completed":
        raise typer.Exit(2)




@think_app.command("chat")
def chat(
    ctx: typer.Context,
    model: str = typer.Option(None, "--model", help="Model id"),
    session_id: str = typer.Option(None, "--session-id", help="Resume an existing session"),
    stream: bool = typer.Option(True, "--stream/--no-stream", help="Stream output live"),
) -> None:
    """Interactive session loop: creates a session when --session-id is not
    supplied, then submits one task per turn bound to that session so turns
    share context. 'exit', 'quit' or Ctrl-D ends the loop.
    """
    if not session_id:
        session_id = _call(ctx, "POST", "/api/v1/session/", json={"title": None}).json()["id"]
        typer.echo(f"(session {session_id})", err=True)
    typer.echo("chat: 'exit' or Ctrl-D to quit", err=True)
    while True:
        try:
            turn = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            typer.echo("")
            break
        if not turn or turn.lower() in {"exit", "quit"}:
            break
        body: dict[str, Any] = {"prompt": turn, "session_id": session_id}
        if model:
            body["model_id"] = model
        try:
            task = _call(ctx, "POST", "/api/v1/task/", json=body).json()
            if stream:
                _watch_stream(ctx, task["id"])
            else:
                _print_task(_await_task(ctx, task["id"]))
            task = _call(ctx, "GET", f"/api/v1/task/{task['id']}").json()
            if task["status"] != "completed":
                typer.echo(f"(turn ended: {task['status']})", err=True)
        except typer.Exit:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the REPL alive on a failed turn
            typer.echo(f"turn failed: {exc}", err=True)


# -- `opal think backup` ---------------------------------------------------------------------------


def _backup_etag(ctx: typer.Context) -> str:
    """Shared portable-configuration revision for backup import/restore."""
    return _call(ctx, "GET", "/api/v1/setting/").headers["ETag"]


def _read_json_payload(json_str: str | None, file: str | None) -> dict[str, Any]:
    """JSON request body from ``--json``, ``--file``, or stdin (in that priority order)."""
    if json_str is not None:
        text = json_str
    elif file is not None:
        text = Path(file).read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        raise typer.BadParameter("provide --json, --file, or pipe JSON on stdin")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"invalid JSON: {exc}") from None


@backup_app.command("list")
def backup_list(ctx: typer.Context) -> None:
    """List service-managed backup archives."""
    data = _call(ctx, "GET", "/api/v1/backup/list").json()
    if _state(ctx).json_output:
        _emit(ctx, data)
        return
    for b in data:
        typer.echo(f"{b['filename']}  {b['size_bytes']} bytes  {b['created_at']}")


@backup_app.command("create")
def backup_create(ctx: typer.Context) -> None:
    """Write a timestamped archive of the current portable configuration."""
    _emit(ctx, _call(ctx, "POST", "/api/v1/backup/create").json())


@backup_app.command("download")
def backup_download(
    ctx: typer.Context,
    filename: str,
    out: str = typer.Option(None, "--out", "-o", help="Write the archive to this path instead of stdout"),
) -> None:
    """Download one service-managed backup archive."""
    data = _call(ctx, "GET", f"/api/v1/backup/download/{filename}").content
    if out:
        Path(out).write_bytes(data)
        typer.echo(f"{out} written ({len(data)} bytes)")
    else:
        sys.stdout.buffer.write(data)


@backup_app.command("restore")
def backup_restore(
    ctx: typer.Context,
    filename: str,
    if_match: str = typer.Option(None, "--if-match", help="ETag precondition (default: auto-fetched)"),
) -> None:
    """Atomically restore a named archive (auto-fetches the config ETag)."""
    etag = if_match or _backup_etag(ctx)
    _emit(ctx, _call(ctx, "POST", f"/api/v1/backup/restore/{filename}", headers={"If-Match": etag}).json())


@backup_app.command("remove")
def backup_remove(ctx: typer.Context, filename: str) -> None:
    """Delete a named backup archive."""
    _call(ctx, "DELETE", f"/api/v1/backup/{filename}")
    typer.echo(f"{filename}: removed")


@backup_app.command("export")
def backup_export(ctx: typer.Context) -> None:
    """Export the portable configuration bundle (settings, personas, profiles)."""
    _emit(ctx, _call(ctx, "GET", "/api/v1/backup/export").json())


@backup_app.command("validate")
def backup_validate(
    ctx: typer.Context,
    json_str: str = typer.Option(None, "--json", help="Bundle as a JSON string"),
    file: str = typer.Option(None, "--file", "-f", help="Bundle from a JSON file"),
) -> None:
    """Validate a configuration bundle without applying it."""
    data = _call(ctx, "POST", "/api/v1/backup/validate", json=_read_json_payload(json_str, file)).json()
    _emit(ctx, data)
    if not data.get("valid"):
        raise typer.Exit(1)


@backup_app.command("import")
def backup_import(
    ctx: typer.Context,
    json_str: str = typer.Option(None, "--json", help="Bundle as a JSON string"),
    file: str = typer.Option(None, "--file", "-f", help="Bundle from a JSON file"),
    no_body: bool = typer.Option(False, "--no-body", help="Re-apply the persisted configuration (self round-trip)"),
    if_match: str = typer.Option(None, "--if-match", help="ETag precondition (default: auto-fetched)"),
) -> None:
    """Atomically import a portable configuration bundle (auto-fetches the config ETag)."""
    etag = if_match or _backup_etag(ctx)
    body = None if no_body else _read_json_payload(json_str, file)
    kwargs: dict[str, Any] = {"headers": {"If-Match": etag}}
    if body is not None:
        kwargs["json"] = body
    _emit(ctx, _call(ctx, "POST", "/api/v1/backup/import", **kwargs).json())


# -- `opal think service` ------------------------------------------------------------------------

LOG_FILE = Path("logs") / "opal_think.log"


def _service_manager(config: str | None = None):
    """Local manager for the REST-unreachable fallback path."""
    from opal_think.lifecycle import ServiceProcessManager

    return ServiceProcessManager(load_config(config))


@service_app.command("start")
def service_start(
    ctx: typer.Context,
    config: str = typer.Option(None, "--config", help="YAML configuration file [env: OPAL_THINK_CONFIG]"),
    append: bool = typer.Option(False, "--append", help="Append to the service log instead of truncating it"),
) -> None:
    """Start the managed server in the background (restarts one already running).

    Calls POST /api/v1/service/start first so remote management works; when the API
    is unreachable it falls back to the same pidfile/log bookkeeping locally, which
    is what lets `service start` cold-start a stopped service."""
    try:
        _emit(ctx, _call(ctx, "POST", "/api/v1/service/start", json={"append": append}).json())
        return
    except ApiCallError as exc:
        if exc.status_code != 0:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
    proc = _service_manager(config).launch(append=append)
    typer.echo(f"opal-think-svc started in background (pid {proc.pid}); logs: {LOG_FILE}")


@service_app.command("stop")
def service_stop(ctx: typer.Context) -> None:
    """Stop the managed server if it is running (not an error when already stopped)."""
    try:
        _emit(ctx, _call(ctx, "POST", "/api/v1/service/stop").json())
        return
    except ApiCallError as exc:
        if exc.status_code != 0:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
    status, _ = _service_manager().stop()
    if status["previous_pid"] is None:
        typer.echo("no managed server running")
    else:
        typer.echo(f"server stopped (pid {status['previous_pid']})")


@service_app.command("log")
def service_log(ctx: typer.Context, lines: int = typer.Option(10, "--lines", "-n", min=1, max=1000)) -> None:
    """Print the trailing lines of the service log."""
    try:
        _emit(ctx, _call(ctx, "GET", "/api/v1/service/log", params={"lines": lines}).json())
        return
    except ApiCallError as exc:
        if exc.status_code == 404:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
        if exc.status_code != 0:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
    tail = _service_manager().tail_log(lines)
    if tail is None:
        typer.echo(f"No log file at {LOG_FILE}; start the service via `service start` first", err=True)
        raise typer.Exit(1)
    typer.echo("".join(line + "\n" for line in tail["lines"]), nl=False)


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



def _param_label(param: click.Parameter) -> str:
    """One tree node: [--flag] for optional, --flag for required, <arg>/[<arg>] for arguments.

    Typer vendors its own click classes, so options are detected by the presence
    of ``opts`` rather than isinstance."""
    opts = list(getattr(param, "opts", None) or []) + list(getattr(param, "secondary_opts", None) or [])
    if any(o.startswith("-") for o in opts):
        label = next((o for o in opts if o.startswith("--")), opts[0])
        text = label if param.required else f"[{label}]"
    else:  # positional argument (vendored click Arguments expose non-dash opts too)
        text = f"<{param.name}>" if param.required else f"[<{param.name}>]"
    if param.help:
        text += f"  # {param.help}"
    return text


def _tree_lines(command: click.Command, prefix: str, lines: list[str]) -> None:
    """Depth-first walk: parameters then subcommands, sorted, ASCII connectors."""
    params = sorted(
        (p for p in getattr(command, "params", []) if "--help" not in (getattr(p, "opts", None) or [])),
        key=lambda p: p.name,
    )
    subs = sorted((getattr(command, "commands", None) or {}).items())
    items: list[tuple[str, object]] = [("param", p) for p in params] + [("cmd", s) for s in subs]
    for index, (kind, item) in enumerate(items):
        last = index == len(items) - 1
        connector = "-- " if last else "|-- "
        if kind == "param":
            lines.append(prefix + connector + _param_label(item))
        else:
            name, sub = item
            lines.append(prefix + connector + name)
            _tree_lines(sub, prefix + ("    " if last else "|   "), lines)


@think_app.command("tree", help="Print the full CLI command/argument tree.")
def tree_cmd() -> None:
    """Render the rooted ASCII tree of every command and argument (skips --help)."""
    lines = ["opal-think"]
    _tree_lines(get_command(think_app), "", lines)
    typer.echo("\n".join(lines))


if __name__ == "__main__":
    sys.exit(think_app())
