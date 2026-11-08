"""`opal think ...` command line. Every command calls the OPAL-THINK REST API over HTTP."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

import click
import typer
from typer.main import get_command

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
sessions_app = typer.Typer(help="Optional conversation context containers.", no_args_is_help=True)
models_app = typer.Typer(help="Discovered model catalog (read-only).", no_args_is_help=True)
tasks_app = typer.Typer(help="Reasoning tasks: create, list, show, watch, cancel.", no_args_is_help=True)
think_app.add_typer(settings_app, name="settings")
think_app.add_typer(sessions_app, name="session")
think_app.add_typer(models_app, name="model")
think_app.add_typer(tasks_app, name="task")

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
    # The collection is a flat OpalSettingsMap; per-key GETs supply default/restart flags.
    for key in sorted(k for k in data if k != "pending_restart_fields"):
        try:
            item = _call(ctx, "GET", f"/api/v1/settings/{key}").json()
        except typer.Exit:
            item = {"key": key, "value": data[key], "is_default": True, "pending_restart": False}
        suffix = "  (default)" if item.get("is_default", True) else "  *"
        suffix += "  [restart pending]" if item.get("pending_restart") else ""
        typer.echo(f"{key} = {json.dumps(item.get('value', data[key]))}{suffix}")
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


# -- session -----------------------------------------------------------------------------------


@sessions_app.command("add")
def session_add(
    ctx: typer.Context,
    title: str = typer.Option(None, "--title", help="Optional human-readable label"),
) -> None:
    """Create a session (a durable conversation context container)."""
    response = _call(ctx, "POST", "/api/v1/sessions/", json={"title": title})
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
    cursor: str = typer.Option(None, "--cursor", help="Continuation token from a previous list"),
) -> None:
    """List sessions, newest updated first."""
    params: dict[str, Any] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    data = _call(ctx, "GET", "/api/v1/sessions/", params=params).json()
    if _state(ctx).json_output:
        _emit(ctx, data)
        return
    for item in data["items"]:
        typer.echo(f"{item['id']}  {item['title'] or '-'}  tasks={item['task_count']}  updated={item['updated_at']}")
    if data["next_cursor"]:
        typer.echo(f"next_cursor: {data['next_cursor']}", err=True)


@sessions_app.command("show")
def session_show(ctx: typer.Context, session_id: str) -> None:
    """Show one session's metadata."""
    _emit(ctx, _call(ctx, "GET", f"/api/v1/sessions/{session_id}").json())


@sessions_app.command("remove")
def session_remove(ctx: typer.Context, session_id: str) -> None:
    """Remove a session and its retained task records (409 while tasks are active)."""
    _call(ctx, "DELETE", f"/api/v1/sessions/{session_id}")
    typer.echo(f"{session_id}: removed")


# -- model -------------------------------------------------------------------------------------


@models_app.command("list")
def model_list(ctx: typer.Context) -> None:
    """List discovered models across configured providers."""
    data = _call(ctx, "GET", "/api/v1/models/").json()
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
    _emit(ctx, _call(ctx, "GET", f"/api/v1/models/{model_id}").json())


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
        data = _call(ctx, "GET", f"/api/v1/tasks/{task_id}").json()
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
    """Submit one unit of work; prints the task id or waits for completion."""
    body: dict[str, Any] = {"prompt": prompt}
    if model:
        body["model_id"] = model
    if session_id:
        body["session_id"] = session_id
    if max_steps:
        body["max_steps"] = max_steps
    if timeout:
        body["timeout_seconds"] = timeout
    data = _call(ctx, "POST", "/api/v1/tasks/", json=body).json()
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
    cursor: str = typer.Option(None, "--cursor"),
) -> None:
    """List retained tasks, newest first."""
    params: dict[str, Any] = {"limit": limit}
    if status:
        params["status"] = status
    if session_id:
        params["session_id"] = session_id
    if cursor:
        params["cursor"] = cursor
    data = _call(ctx, "GET", "/api/v1/tasks/", params=params).json()
    if _state(ctx).json_output:
        _emit(ctx, data)
        return
    for t in data["items"]:
        typer.echo(f"{t['id']}  {t['status']:<10}  {t['model_id']}  {t['prompt'][:60]}")
    if data["next_cursor"]:
        typer.echo(f"next_cursor: {data['next_cursor']}", err=True)


@tasks_app.command("show")
def task_show(ctx: typer.Context, task_id: str) -> None:
    """Authoritative snapshot: status, answer, bounded steps."""
    _emit(ctx, _call(ctx, "GET", f"/api/v1/tasks/{task_id}").json())


def _watch_stream(ctx: typer.Context, task_id: str) -> dict | None:
    """Follow the task SSE stream to a terminal event; returns terminal frame data."""
    state = _state(ctx)
    json_mode = state.json_output
    with state.client.stream(f"/api/v1/tasks/{task_id}/stream") as response:
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
    data = _call(ctx, "GET", f"/api/v1/tasks/{task_id}").json()
    if _state(ctx).json_output:
        _emit(ctx, data)
    elif not data.get("answer"):
        typer.echo(f"status={data['status']}" + (f"  error={data['error']['message']}" if data.get("error") else ""))
    if data["status"] != "completed":
        raise typer.Exit(2)


@tasks_app.command("cancel")
def task_cancel(ctx: typer.Context, task_id: str) -> None:
    """Request cooperative cancellation (terminal tasks return 409)."""
    data = _call(ctx, "POST", f"/api/v1/tasks/{task_id}/cancel").json()
    if _state(ctx).json_output:
        _emit(ctx, data)
    else:
        typer.echo(f"{data['id']}  status={data['status']}  cancel_requested={data['cancel_requested']}")


@think_app.command("ask")
def ask(
    ctx: typer.Context,
    prompt: str,
    model: str = typer.Option(None, "--model", help="Model id"),
    session_id: str = typer.Option(None, "--session-id", help="Existing session id"),
    stream: bool = typer.Option(True, "--stream/--no-stream", help="Stream output live"),
) -> None:
    """One-shot task: create, watch the stream, print the answer."""
    body: dict[str, Any] = {"prompt": prompt}
    if model:
        body["model_id"] = model
    if session_id:
        body["session_id"] = session_id
    data = _call(ctx, "POST", "/api/v1/tasks/", json=body).json()
    if stream:
        _watch_stream(ctx, data["id"])
    data = _await_task(ctx, data["id"]) if not stream else _call(ctx, "GET", f"/api/v1/tasks/{data['id']}").json()
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
    """Session-based loop: creates a session when none is supplied; per turn submits a task."""
    if not session_id:
        session_id = _call(ctx, "POST", "/api/v1/sessions/", json={"title": None}).json()["id"]
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
            task = _call(ctx, "POST", "/api/v1/tasks/", json=body).json()
            if stream:
                _watch_stream(ctx, task["id"])
            else:
                _print_task(_await_task(ctx, task["id"]))
            task = _call(ctx, "GET", f"/api/v1/tasks/{task['id']}").json()
            if task["status"] != "completed":
                typer.echo(f"(turn ended: {task['status']})", err=True)
        except typer.Exit:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the REPL alive on a failed turn
            typer.echo(f"turn failed: {exc}", err=True)


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
