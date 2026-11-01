"""Task endpoints (``/api/v1/tasks/`` family).

Admission is asynchronous: POST returns 202 + Location, never a completed answer. Session
links serialize active work (409 while busy); Idempotency-Key dedupes admission; the
concurrency ceiling yields 429; causation depth is bounded to prevent trigger loops.
Stream and cancel live in their own routes; there is deliberately no task deletion.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response

from opal_think.deps import get_context, get_engine, get_store
from opal_think.engine import TaskEngine
from opal_think.errors import OpalHTTPException
from opal_think.opal_context import OpalContext
from opal_think.pagination import decode_scoped_cursor, encode_scoped_cursor
from opal_think.schemas import Task, TaskCreate, TaskPage, TaskStatus
from opal_think.store import ThinkStore

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

TaskId = Path(min_length=1, max_length=128)


def _to_task(row: dict[str, Any]) -> Task:
    return Task(
        id=row["id"],
        status=row["status"],
        prompt=row["prompt"],
        metadata=row.get("metadata") or {},
        session_id=row["session_id"],
        model_id=row["model_id"],
        max_steps=row["max_steps"],
        timeout_seconds=row["timeout_seconds"],
        steps_completed=row["steps_completed"],
        cancel_requested=bool(row["cancel_requested"]),
        answer=row["answer"],
        error=row.get("error"),
        steps=row.get("steps") or [],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _get_or_404(store: ThinkStore, task_id: str) -> dict[str, Any]:
    row = store.get_task(task_id)
    if row is None:
        raise OpalHTTPException(404, "not_found", f"task not found: {task_id}")
    return row


@router.post("/", status_code=202, operation_id="create_task", summary="Create task")
async def create_task(
    body: TaskCreate,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=128),
    engine: TaskEngine = Depends(get_engine),
    store: ThinkStore = Depends(get_store),
    ctx: OpalContext = Depends(get_context),
) -> Task:
    if body.session_id and store.get_session(body.session_id) is None:
        raise OpalHTTPException(404, "not_found", f"session not found: {body.session_id}")
    depth = engine.causation_depth(ctx.causation_id)
    if depth > engine.setting("task.max_causation_depth"):
        raise OpalHTTPException(
            422, "causation_depth_exceeded", "causation chain depth limit exceeded; refusing nested trigger"
        )
    adapter, _, resolved_id = await engine.resolve_model(body.model_id)
    row, replayed = engine.admit(
        body,
        resolved_model_id=resolved_id,
        causation_id=ctx.causation_id,
        causation_depth=depth,
        idempotency_key=idempotency_key,
    )
    response.headers["Location"] = str(request.url_for("show_task", task_id=row["id"]))
    if not replayed:
        engine.start(adapter, row)
    return _to_task(row)


@router.get("/{task_id}", operation_id="show_task", summary="Show task")
def show_task(task_id: str = TaskId, store: ThinkStore = Depends(get_store)) -> Task:
    return _to_task(_get_or_404(store, task_id))


@router.get("/", operation_id="list_tasks", summary="List retained tasks")
def list_tasks(
    status: TaskStatus | None = Query(default=None),
    session_id: str | None = Query(default=None, min_length=1),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, min_length=1),
    store: ThinkStore = Depends(get_store),
) -> TaskPage:
    filters = {"status": status.value if status else None, "session_id": session_id}
    inner = decode_scoped_cursor(cursor, filters) if cursor else None
    rows, next_inner = store.list_tasks(limit, inner, status=status.value if status else None, session_id=session_id)
    return TaskPage(
        items=[_to_task(row) for row in rows],
        next_cursor=encode_scoped_cursor(next_inner, filters) if next_inner else None,
        has_more=next_inner is not None,
    )
