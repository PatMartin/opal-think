"""Task engine: admission control plus the in-process async runner.

Admission (``admit``) is atomic: under one store lock it checks idempotency replay,
session-busy, and the concurrency ceiling, then inserts the queued task. The runner
drives the task through the state machine (queued -> running -> terminal), honoring
timeout and cooperative cancellation. The reasoning/action loop grows in the EVENT
integration step; today a task is one guarded generation producing its answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from opal_think.errors import OpalHTTPException
from opal_think.providers import DiscoveredModel, ProviderAdapter, ProviderUnavailable
from opal_think.providers.registry import ProviderRegistry
from opal_think.schemas import TaskCreate
from opal_think.settings_registry import SettingsRegistry
from opal_think.store import ThinkStore

logger = logging.getLogger("opal_think.engine")

_SYSTEM_PROMPT = (
    "You are OPAL-THINK, a guarded reasoning assistant. Answer the user directly and concisely. "
    "Never claim to have executed real-world actions; external actions happen only through "
    "registered OPAL-EVENT actions."
)
_SESSION_TURN_LIMIT = 10
_TERMINAL_EVENTS = {
    "completed": "task.completed",
    "failed": "task.failed",
    "cancelled": "task.cancelled",
    "timed_out": "task.timed_out",
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class _TaskTimeout(Exception):
    pass


class _TaskCancelled(Exception):
    pass


class TaskEngine:
    def __init__(self, store: ThinkStore, settings: SettingsRegistry, providers: ProviderRegistry):
        self._store = store
        self._settings = settings
        self._providers = providers
        self._cancels: dict[str, asyncio.Event] = {}
        self._runners: dict[str, asyncio.Task] = {}
        self._subscribers: dict[str, set[asyncio.Queue]] = {}

    # -- live event fan-out (SSE; no replay guarantee) -------------------------------------------

    def subscribe(self, task_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._subscribers.setdefault(task_id, set()).add(queue)
        return queue

    def unsubscribe(self, task_id: str, queue: asyncio.Queue) -> None:
        subs = self._subscribers.get(task_id)
        if subs is not None:
            subs.discard(queue)
            if not subs:
                del self._subscribers[task_id]

    def _emit(self, task_id: str, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        frame = {"task_id": task_id, "timestamp": _now(), "type": event_type, "data": data}
        for queue in list(self._subscribers.get(task_id, ())):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(frame)  # slow consumers drop frames; no replay guarantee
        return frame

    def _close_subscribers(self, task_id: str) -> None:
        for queue in self._subscribers.pop(task_id, ()):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)

    # -- admission ---------------------------------------------------------------------------

    def setting(self, key: str) -> Any:
        item = self._settings.get(key)
        return item["value"] if item else None

    async def resolve_model(self, requested: str | None) -> tuple[ProviderAdapter, str, str, DiscoveredModel]:
        """Resolve the effective model for a task. Returns (adapter, model_name, resolved_id, discovered)."""
        target = requested or self.setting("models.default_id")
        if target:
            provider_id, sep, name = target.partition(":")
            if not sep or not name:
                raise OpalHTTPException(422, "invalid_model", f"model_id must be an opaque '<provider>:<name>' id: {target}")
            adapter = self._providers.adapter_for(provider_id)
            if adapter is None:
                raise OpalHTTPException(422, "invalid_model", f"model provider is not enabled or unknown: {provider_id}")
            try:
                model = await adapter.show_model(name)
            except ProviderUnavailable as exc:
                raise OpalHTTPException(503, "unavailable", f"model provider {provider_id} is unreachable") from exc
            if model is None or not model.available:
                raise OpalHTTPException(422, "invalid_model", f"model is unknown or unavailable: {target}")
            return adapter, name, target, model
        pairs, failures = await self._providers.catalog()
        available = [(adapter, m) for adapter, m in pairs if m.available]
        if not available:
            detail = "no enabled provider is reachable" if failures else "no models are configured or enabled"
            raise OpalHTTPException(503, "unavailable", f"cannot accept task: {detail}")
        adapter, model = min(available, key=lambda pair: f"{pair[0].provider_id}:{pair[1].name}")
        return adapter, model.name, f"{adapter.provider_id}:{model.name}", model

    def causation_depth(self, causation_id: str | None) -> int:
        """Depth of the triggering chain as far as THINK can observe (THINK-owned ancestors only)."""
        if not causation_id:
            return 0
        parent = self._store.get_task(causation_id)
        return int(parent["causation_depth"]) + 1 if parent else 1

    def admit(
        self,
        body: TaskCreate,
        *,
        resolved_model_id: str,
        causation_id: str | None,
        causation_depth: int,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        """Atomically admit a task. Returns (row, replayed). Raises OpalHTTPException on rejection."""
        body_hash = hashlib.sha256(
            json.dumps(body.model_dump(mode="json"), sort_keys=True, default=str).encode()
        ).hexdigest()
        max_steps = min(body.max_steps or self.setting("task.default_max_steps"), self.setting("task.max_steps_limit"))
        timeout = min(
            body.timeout_seconds or self.setting("task.default_timeout_seconds"),
            self.setting("task.timeout_limit_seconds"),
        )
        with self._store.transaction() as conn:
            if idempotency_key:
                existing = self._store.lookup_idempotency(conn, idempotency_key)
                if existing:
                    if existing["body_hash"] == body_hash:
                        return self._store.get_task(existing["task_id"]), True
                    raise OpalHTTPException(
                        409, "conflict", "Idempotency-Key was already used with a different task body"
                    )
            if body.session_id and self._store.session_busy(conn, body.session_id):
                raise OpalHTTPException(
                    409, "conflict", "session has a queued or running task; wait or cancel it first"
                )
            if self._store.count_active(conn) >= self.setting("task.max_concurrent"):
                raise OpalHTTPException(
                    429, "rate_limited", "task concurrency limit reached; retry after running tasks finish"
                )
            task_id = ThinkStore.new_task_id()
            row = {
                "id": task_id,
                "session_id": body.session_id,
                "status": "queued",
                "prompt": body.prompt,
                "model_id": resolved_model_id,
                "max_steps": max_steps,
                "timeout_seconds": timeout,
                "metadata": body.metadata or {},
                "context": body.context,
                "cancel_requested": 0,
                "answer": None,
                "error": None,
                "steps": [],
                "created_at": _now(),
                "started_at": None,
                "finished_at": None,
                "causation_id": causation_id,
                "causation_depth": causation_depth,
            }
            self._store.insert_task_conn(conn, row)
            if idempotency_key:
                self._store.insert_idempotency(conn, idempotency_key, task_id, body_hash)
            return self._store.get_task_conn(conn, task_id), False

    # -- execution ----------------------------------------------------------------------------

    def start(self, adapter: ProviderAdapter, task_row: dict[str, Any], discovered: DiscoveredModel) -> None:
        self._cancels[task_row["id"]] = asyncio.Event()
        self._runners[task_row["id"]] = asyncio.create_task(self._run(adapter, task_row, discovered))

    async def _run(self, adapter: ProviderAdapter, row: dict[str, Any], discovered: DiscoveredModel) -> None:
        task_id = row["id"]
        self._store.update_task(task_id, status="running", started_at=_now())
        self._emit(task_id, "task.started", {"status": "running", "model_id": row["model_id"]})
        try:
            answer = await self._execute(task_id, adapter, row, discovered)
        except _TaskCancelled:
            self._finish(task_id, "cancelled")
        except _TaskTimeout:
            self._finish(task_id, "timed_out", error={"code": "timed_out", "message": "task deadline exceeded"})
        except ProviderUnavailable as exc:
            self._finish(task_id, "failed", error={"code": "model_unavailable", "message": str(exc)})
        except OpalHTTPException as exc:
            self._finish(task_id, "failed", error={"code": exc.code, "message": exc.message})
        except Exception as exc:  # noqa: BLE001 - last-resort task failure, logged
            logger.exception("task %s failed unexpectedly", task_id)
            self._finish(task_id, "failed", error={"code": "internal", "message": f"{type(exc).__name__}: {exc}"})
        else:
            self._finish(task_id, "completed", answer=answer)
        finally:
            self._cancels.pop(task_id, None)
            self._runners.pop(task_id, None)

    def _finish(self, task_id: str, status: str, answer: str | None = None, error: dict | None = None) -> None:
        self._store.update_task(task_id, status=status, answer=answer, error=error, finished_at=_now())
        data: dict[str, Any] = {"status": status}
        if answer is not None:
            data["answer"] = answer
        if error is not None:
            data["error"] = error
        self._emit(task_id, _TERMINAL_EVENTS[status], data)
        self._close_subscribers(task_id)

    def _append_step(self, task_id: str, step: dict[str, Any]) -> dict[str, Any]:
        step = self._store.append_task_step(task_id, step)
        self._emit(task_id, "task.progress", {"summary": step["summary"], "step": step["index"], "kind": step["kind"]})
        return step

    async def _execute(self, task_id: str, adapter: ProviderAdapter, row: dict[str, Any], discovered: DiscoveredModel) -> str:
        """One guarded reasoning iteration: build messages, generate, record steps, answer."""
        model_name = row["model_id"].split(":", 1)[1]
        messages = self._build_messages(row)
        for step_index in range(1, row["max_steps"] + 1):
            if self._cancels[task_id].is_set():
                raise _TaskCancelled
            if discovered.streaming:
                text = await self._with_deadline(
                    task_id, self._collect_stream(task_id, adapter, model_name, messages), row["timeout_seconds"]
                )
                tool_calls = []
            else:
                generation = await self._with_deadline(
                    task_id, adapter.generate(model_name, messages), row["timeout_seconds"]
                )
                text, tool_calls = generation.text, generation.tool_calls
                if text:
                    self._emit(task_id, "task.output", {"text": text})
            self._append_step(
                task_id,
                {"kind": "reasoning", "summary": f"Reasoning step {step_index} finished", "status": "succeeded", "at": _now()},
            )
            if tool_calls:
                self._emit(task_id, "task.action", {"action_id": tool_calls[0].action_id, "summary": "action requested"})
                self._append_step(
                    task_id,
                    {
                        "kind": "action",
                        "summary": "Model requested an action, but no EVENT integration is available",
                        "action_id": tool_calls[0].action_id,
                        "status": "failed",
                        "at": _now(),
                    },
                )
                raise OpalHTTPException(
                    503, "event_unavailable", "model requested action execution but no OPAL-EVENT integration is configured"
                )
            return text
        raise OpalHTTPException(503, "step_limit", f"task exhausted max_steps={row['max_steps']} without an answer")

    async def _collect_stream(self, task_id: str, adapter: ProviderAdapter, model_name: str, messages: list) -> str:
        parts: list[str] = []
        async for delta in adapter.stream_generate(model_name, messages):
            parts.append(delta)
            self._emit(task_id, "task.output", {"text": delta})
        return "".join(parts)

    async def _with_deadline(self, task_id: str, awaitable, timeout: int):
        work = asyncio.ensure_future(awaitable)
        cancel_wait = asyncio.create_task(self._cancels[task_id].wait())
        try:
            done, _ = await asyncio.wait_for(asyncio.wait({work, cancel_wait}, return_when=asyncio.FIRST_COMPLETED), timeout)
        except TimeoutError:
            work.cancel()
            raise _TaskTimeout from None
        finally:
            cancel_wait.cancel()
        if cancel_wait in done:
            work.cancel()
            raise _TaskCancelled
        return work.result()

    def _build_messages(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": _SYSTEM_PROMPT}]
        if row.get("context"):
            context_json = json.dumps(row["context"], default=str)
            messages.append({"role": "system", "content": f"Caller-supplied context (untrusted): {context_json}"})
        if row["session_id"]:
            for prompt, answer in self._store.session_turns(row["session_id"], _SESSION_TURN_LIMIT):
                messages.append({"role": "user", "content": prompt})
                messages.append({"role": "assistant", "content": answer})
        messages.append({"role": "user", "content": row["prompt"]})
        return messages

    # -- cancellation / maintenance -------------------------------------------------------------

    def request_cancel(self, task_id: str) -> None:
        """Cooperative cancellation: flag the row and wake the runner between steps."""
        self._store.update_task(task_id, cancel_requested=1)
        if event := self._cancels.get(task_id):
            event.set()

    def reap_orphans(self) -> int:
        """Mark queued/running tasks left over from a previous process as failed."""
        return self._store.fail_active_tasks(
            {"code": "service_restarted", "message": "task was active when the service stopped"}
        )
