"""Session endpoints (``/api/v1/session/`` and ``/api/v1/session/{session_id}``).

Optional durable conversation context: POST creates (201+Location), GET lists
newest-updated-first with expiring cursors, DELETE removes the session AND its
retained linked task records (409 while any linked task is queued/running).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from opal_think.deps import get_store
from opal_think.errors import OpalHTTPException
from opal_think.schemas import Session, SessionCreate
from opal_think.store import ThinkStore

router = APIRouter(prefix="/api/v1/session", tags=["sessions"])

SessionId = Path(min_length=1, max_length=128)


def _session_payload(row: dict) -> Session:
    return Session(
        id=row["id"],
        title=row["title"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        task_count=row["task_count"],
    )


def _get_or_404(store: ThinkStore, session_id: str) -> dict:
    row = store.get_session(session_id)
    if row is None:
        raise OpalHTTPException(404, "not_found", f"session not found: {session_id}")
    return row


@router.post("/", status_code=201, operation_id="add_session", summary="Add session")
def add_session(
    body: SessionCreate,
    request: Request,
    response: Response,
    store: ThinkStore = Depends(get_store),
) -> Session:
    row = store.add_session(body.title)
    response.headers["Location"] = str(request.url_for("show_session", session_id=row["id"]))
    return _session_payload(row)


@router.get("/", operation_id="list_sessions", summary="List sessions")
def list_sessions(
    limit: int = Query(default=50, ge=1, le=200),
    store: ThinkStore = Depends(get_store),
) -> list[Session]:
    rows = store.list_sessions(limit)
    return [_session_payload(row) for row in rows]


@router.get("/{session_id}", operation_id="show_session", summary="Show session")
def show_session(session_id: str = SessionId, store: ThinkStore = Depends(get_store)) -> Session:
    return _session_payload(_get_or_404(store, session_id))


@router.delete("/{session_id}", operation_id="remove_session", summary="Remove session", status_code=204)
def remove_session(session_id: str = SessionId, store: ThinkStore = Depends(get_store)) -> Response:
    _get_or_404(store, session_id)
    if store.session_has_active_tasks(session_id):
        raise OpalHTTPException(
            409, "conflict", "session has queued or running tasks; cancel them before removing the session"
        )
    store.delete_session(session_id)
    return Response(status_code=204)
