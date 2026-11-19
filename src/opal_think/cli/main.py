"""`opal think ...` command line. Every command calls the OPAL-THINK REST API over HTTP."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.console import Console
from rich.text import Text
from rich.tree import Tree
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
info_app = typer.Typer(help="Service health probe.", no_args_is_help=True)
service_app = typer.Typer(help="Managed opal-think-svc process lifecycle.", no_args_is_help=True)
# Canonical x-opal-cli bindings (v0.1.0): `config` hosts save/load/validate and the
# `setting` group; `persona` and `profile` are top-level groups.
config_app = typer.Typer(help="Portable configuration resources.", no_args_is_help=True)
config_setting_app = typer.Typer(help="Portable service settings (strong-ETag guarded).", no_args_is_help=True)
personas_app = typer.Typer(help="Named assistant personas (shared OPAL identities).", no_args_is_help=True)
profiles_app = typer.Typer(help="Namespaced profiles (shared + module compartments).", no_args_is_help=True)
config_app.add_typer(config_setting_app, name="setting")
think_app.add_typer(sessions_app, name="session")
think_app.add_typer(models_app, name="model")
think_app.add_typer(tasks_app, name="task")
think_app.add_typer(info_app, name="info")
think_app.add_typer(config_app, name="config")
think_app.add_typer(personas_app, name="persona")
think_app.add_typer(profiles_app, name="profile")
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
    env_file: str = typer.Option(None, "--env-file", help="Load this dotenv file instead of ./.env"),
    no_env_file: bool = typer.Option(False, "--no-env-file", help="Disable dotenv loading and .env initialization"),
) -> None:
    from opal_think.config import dotenv_baseline
    from opal_think.dotenv import DotenvError, prepare

    try:
        prepare(env_file=env_file, no_env_file=no_env_file, baseline=dotenv_baseline())
    except DotenvError as exc:
        raise typer.BadParameter(str(exc)) from exc
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


@think_app.command("gui")
def gui(ctx: typer.Context) -> None:
    """Open the SPA in a browser (served by the running service at its base URL)."""
    import webbrowser

    state = _state(ctx)
    try:
        state.client.get("/api/v1/health/live", timeout=2.0)
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


# -- profile (kernel v3 namespaced profiles; top-level group) ------------------------------------------


def _profile_etag(ctx: typer.Context, profile_id: str) -> str:
    return _call(ctx, "GET", f"/api/v1/profile/{profile_id}").headers["ETag"]


def _profile_document(
    in_file: str | None,
    name: str | None,
    description: str | None,
    is_active: bool | None,
    shared: str | None,
    module: str | None,
) -> dict[str, Any]:
    """Build the profile document/patch per the kernel ``x-opal-cli`` bindings: ``--in``
    supplies the whole file (exclusive with the field options); ``--shared``/
    ``--module`` files feed the ``shared`` section and the own ``opal-think``
    compartment."""
    if in_file is not None and any(v is not None for v in (name, description, is_active, shared, module)):
        typer.echo("error: --in is exclusive with --name/--description/--is-active/--shared/--module", err=True)
        raise typer.Exit(2)
    if in_file is not None:
        return _read_snapshot_file(in_file)
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if is_active is not None:
        body["is_active"] = is_active
    if shared is not None:
        body["shared"] = _read_snapshot_file(shared)
    if module is not None:
        body.setdefault("modules", {})["opal-think"] = _read_snapshot_file(module)
    return body


_PROFILE_IS_ACTIVE = typer.Option(None, "--is-active/--no-is-active", help="Activate this profile locally")


@profiles_app.command("list")
def profile_list(ctx: typer.Context) -> None:
    """List profiles (complete bounded catalog; no pagination)."""
    data = _call(ctx, "GET", "/api/v1/profile/").json()
    if _state(ctx).json_output:
        _emit(ctx, data)
        return
    for p in data:
        active = " *active*" if p.get("is_active") else ""
        modules = ",".join(sorted((p.get("modules") or {}).keys())) or "-"
        typer.echo(f"{p['id']}  {p['name']}{active}  modules={modules}")


@profiles_app.command("add")
def profile_add(
    ctx: typer.Context,
    in_file: str = typer.Option(None, "--in", help="Whole portable profile document (YAML/JSON)"),
    name: str = typer.Option(None, "--name"),
    description: str = typer.Option(None, "--description"),
    is_active: bool | None = _PROFILE_IS_ACTIVE,
    shared: str = typer.Option(None, "--shared", help="Shared-values object file"),
    module: str = typer.Option(None, "--module", help="opal-think compartment file"),
    profile_id: str = typer.Option(None, "--id", help="Client-supplied opaque profile id"),
    if_match: str = typer.Option(None, "--if-match", help="ETag precondition (default: auto-fetched)"),
) -> None:
    """Add a profile (auto-fetches the config ETag)."""
    body = _profile_document(in_file, name, description, is_active, shared, module)
    if profile_id is not None:
        body["id"] = profile_id
    if "name" not in body:
        typer.echo("error: a profile name is required (--name or --in document)", err=True)
        raise typer.Exit(2)
    etag = if_match or _config_etag(ctx)
    _emit(ctx, _call(ctx, "POST", "/api/v1/profile/", json=body, headers={"If-Match": etag}).json())


@profiles_app.command("show")
def profile_show(ctx: typer.Context, profile_id: str) -> None:
    """Show one profile, including all module compartments."""
    _emit(ctx, _call(ctx, "GET", f"/api/v1/profile/{profile_id}").json())


@profiles_app.command("update")
def profile_update(
    ctx: typer.Context,
    profile_id: str,
    in_file: str = typer.Option(None, "--in", help="Patch document (YAML/JSON)"),
    name: str = typer.Option(None, "--name"),
    description: str = typer.Option(None, "--description"),
    is_active: bool | None = _PROFILE_IS_ACTIVE,
    shared: str = typer.Option(None, "--shared", help="Shared-values object file"),
    module: str = typer.Option(None, "--module", help="Partial opal-think compartment file"),
    if_match: str = typer.Option(None, "--if-match", help="ETag precondition (default: auto-fetched)"),
) -> None:
    """Partially update a profile (auto-fetches the config ETag)."""
    body = _profile_document(in_file, name, description, is_active, shared, module)
    if not body:
        typer.echo("error: nothing to update (pass --in or at least one field option)", err=True)
        raise typer.Exit(1)
    etag = if_match or _profile_etag(ctx, profile_id)
    _emit(ctx, _call(ctx, "PATCH", f"/api/v1/profile/{profile_id}", json=body, headers={"If-Match": etag}).json())


@profiles_app.command("remove")
def profile_remove(
    ctx: typer.Context,
    profile_id: str,
    if_match: str = typer.Option(None, "--if-match", help="ETag precondition (default: auto-fetched)"),
) -> None:
    """Remove a profile (409 while it is the active profile; auto-fetches the config ETag)."""
    etag = if_match or _profile_etag(ctx, profile_id)
    _call(ctx, "DELETE", f"/api/v1/profile/{profile_id}", headers={"If-Match": etag})
    typer.echo(f"{profile_id}: removed")


@profiles_app.command("activate")
def profile_activate(
    ctx: typer.Context,
    profile_id: str,
    if_match: str = typer.Option(None, "--if-match", help="ETag precondition (default: auto-fetched)"),
) -> None:
    """Make this profile the only active one (local selection; empty opal-think
    compartment means no settings change)."""
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


@tasks_app.command("add")
@tasks_app.command("create", hidden=True)
def task_add(
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


@personas_app.command("list")
def persona_list(
    ctx: typer.Context,
    enabled: bool = typer.Option(None, "--enabled/--no-enabled", help="Filter by enabled state"),
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


@personas_app.command("add")
def persona_create(
    ctx: typer.Context,
    persona_id: str = typer.Option(..., "--persona-id", help="Globally unique OPAL persona identifier"),
    name: str = typer.Option(..., "--name", help="Human-readable persona name"),
    description: str = typer.Option(None, "--description", help="Optional character notes"),
    voice_id: str = typer.Option(None, "--voice-id", help="OPAL Say voice binding"),
    enabled: bool = typer.Option(True, "--enabled/--no-enabled", help="Create enabled (default) or disabled"),
    if_match: str = typer.Option(None, "--if-match", help="ETag precondition (default: auto-fetched)"),
) -> None:
    """Register a persona (auto-fetches the configuration ETag)."""
    body: dict[str, Any] = {"persona_id": persona_id, "name": name, "enabled": enabled}
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


@personas_app.command("show")
def persona_show(ctx: typer.Context, persona_id: str) -> None:
    """Show one registered persona."""
    _emit(ctx, _call(ctx, "GET", f"/api/v1/persona/{persona_id}").json())


@personas_app.command("update")
def persona_update(
    ctx: typer.Context,
    persona_id: str,
    name: str = typer.Option(None, "--name", help="New human-readable name"),
    description: str = typer.Option(None, "--description", help="New description"),
    voice_id: str = typer.Option(None, "--voice-id", help="Rebind to a different OPAL Say voice"),
    clear_description: bool = typer.Option(False, "--clear-description", help="Set description to null"),
    clear_voice: bool = typer.Option(False, "--clear-voice", help="Remove the voice binding"),
    enabled: bool = typer.Option(None, "--enabled/--no-enabled", help="Set enabled state"),
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


@personas_app.command("remove")
def persona_remove(
    ctx: typer.Context,
    persona_id: str,
    if_match: str = typer.Option(None, "--if-match", help="ETag precondition (default: auto-fetched)"),
) -> None:
    """Remove a persona registration (auto-fetches the configuration ETag)."""
    etag = if_match or _persona_etag(ctx, persona_id)
    _call(ctx, "DELETE", f"/api/v1/persona/{persona_id}", headers={"If-Match": etag})
    typer.echo(f"{persona_id}: removed")


@personas_app.command("validate")
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

    Shortcut for `task add` + `task watch`. No session is created unless
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


# -- `opal think config` (kernel config save|load|validate) -----------------------------------------------


def _read_snapshot_file(path: str) -> dict[str, Any]:
    """Read a local YAML or JSON snapshot file (kernel ``yaml_or_json_file`` binding)."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        typer.echo(f"error: cannot read snapshot file {path}: {exc}", err=True)
        raise typer.Exit(2) from None
    if not isinstance(data, dict):
        typer.echo(f"error: snapshot file {path} is not a mapping", err=True)
        raise typer.Exit(2)
    return data


@config_app.command("save")
def config_save(
    ctx: typer.Context,
    out: str = typer.Option(None, "--out", help="Write the snapshot to FILE instead of the managed store"),
) -> None:
    """Capture the persistent configuration as a portable snapshot."""
    if out is None:
        data = _call(ctx, "POST", "/api/v1/config/save", json={"destination": "managed"}).json()
        typer.echo("managed snapshot saved", err=True)
        _emit(ctx, data["snapshot"])
    else:
        data = _call(ctx, "POST", "/api/v1/config/save", json={"destination": "response"}).json()
        Path(out).write_text(json.dumps(data["snapshot"], indent=2), encoding="utf-8")
        typer.echo(f"snapshot written to {out}", err=True)


@config_app.command("load")
def config_load_cmd(
    ctx: typer.Context,
    in_file: str = typer.Option(None, "--in", help="YAML or JSON snapshot file (omit to load the managed snapshot)"),
    if_match: str = typer.Option(None, "--if-match", help="ETag precondition (default: auto-fetched)"),
) -> None:
    """Atomically replace the persistent portable configuration with a snapshot."""
    body = {"snapshot": _read_snapshot_file(in_file)} if in_file else {}
    etag = if_match or _call(ctx, "GET", "/api/v1/setting/").headers["ETag"]
    _emit(ctx, _call(ctx, "POST", "/api/v1/config/load", json=body, headers={"If-Match": etag}).json())


@config_app.command("validate")
def config_validate_cmd(
    ctx: typer.Context,
    in_file: str = typer.Option(None, "--in", help="YAML or JSON snapshot file (omit to validate live configuration)"),
) -> None:
    """Validate a candidate snapshot, or the live configuration, without side effects."""
    body = {"snapshot": _read_snapshot_file(in_file)} if in_file else {}
    data = _call(ctx, "POST", "/api/v1/config/validate", json=body).json()
    _emit(ctx, data)
    if not data["valid"]:
        raise typer.Exit(1)


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



# -- `opal think tree` --------------------------------------------------------------------------
# Mirrors `opal-listen`'s tree renderer: parameters are styled usage labels appended to each
# command's own label (yellow when required, dim when bracketed), not separate child nodes.


def _param_label(param: object) -> tuple[str, str] | None:
    """A param's ``(text, style)`` — yellow when required, dim when bracketed — or None to skip it."""
    if getattr(param, "hidden", False) or getattr(param, "expose_value", True) is False:
        return None
    kind = getattr(param, "param_type_name", "")
    required = getattr(param, "required", False)
    style = "yellow" if required else "dim"
    if kind == "argument":
        name = param.name  # type: ignore[attr-defined]
        return (name if required else f"[{name}]", style)
    if kind != "option":
        return None
    opts = [*getattr(param, "opts", ()), *getattr(param, "secondary_opts", ())]
    flag = next((o for o in opts if o.startswith("--")), opts[0] if opts else param.name)  # type: ignore[attr-defined]
    if flag == "--help":
        return None
    return (flag if required else f"[{flag}]", style)


def _usage_labels(cmd: object) -> list[tuple[str, str]]:
    return [label for p in getattr(cmd, "params", ()) if (label := _param_label(p))]


def _visible_commands(group: object) -> list[tuple[str, object]]:
    return [(name, cmd) for name, cmd in getattr(group, "commands", {}).items() if not getattr(cmd, "hidden", False)]


def _is_group(cmd: object) -> bool:
    return bool(getattr(cmd, "commands", None))


def _styled_label(name: str, cmd: object, name_style: str) -> Text:
    text = Text(name, style=name_style)
    for label, style in _usage_labels(cmd):
        text.append(f" {label}", style=style)
    return text


def _plain_label(name: str, cmd: object) -> str:
    return " ".join([name, *(label for label, _ in _usage_labels(cmd))])


def _add_commands(node: Tree, group: object) -> None:
    for name, cmd in _visible_commands(group):
        child = node.add(_styled_label(name, cmd, "bold cyan" if _is_group(cmd) else "cyan"), guide_style="blue")
        if _is_group(cmd):
            _add_commands(child, cmd)


def _rich_tree(root: object, label: str) -> Tree:
    """Render a click Command/Group as a styled rich Tree rooted at ``label``."""
    tree = Tree(_styled_label(label, root, "bold green"), guide_style="blue")
    _add_commands(tree, root)
    return tree


def _ascii_lines(group: object, prefix: str) -> list[str]:
    visible = _visible_commands(group)
    lines: list[str] = []
    for i, (name, cmd) in enumerate(visible):
        last = i == len(visible) - 1
        lines.append(f"{prefix}{'`-- ' if last else '|-- '}{_plain_label(name, cmd)}")
        if _is_group(cmd):
            lines.extend(_ascii_lines(cmd, prefix + ("    " if last else "|   ")))
    return lines


def _ascii_tree(root: object, label: str) -> str:
    """Render a click Command/Group as a color-free ASCII tree rooted at ``label``."""
    lines = [_plain_label(label, root)]
    lines.extend(_ascii_lines(root, ""))
    return "\n".join(lines)


@think_app.command("tree", help="Print the full CLI command/argument tree.")
def tree_cmd(
    ascii_guides: bool = typer.Option(
        False, "--ascii", help="ASCII guides, no color (safe for logs and pipes)."
    ),
) -> None:
    """Render the command/argument tree — rich colors on a terminal, plain
    Unicode box-drawing when piped; ``--ascii`` for a pure-ASCII variant."""
    root = get_command(think_app)
    if ascii_guides:
        typer.echo(_ascii_tree(root, "opal-think"))
    else:
        Console().print(_rich_tree(root, "opal-think"))


if __name__ == "__main__":
    sys.exit(think_app())
