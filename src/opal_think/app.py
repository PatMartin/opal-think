"""FastAPI application factory: middleware, exception handlers, router wiring.

Every error response uses the nested ``OpalError`` envelope per ``opal_think.v1.4.0.yaml``;
correlation headers are applied to all responses by :class:`OpalContextMiddleware`.
"""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from opal_think import API_VERSION
from opal_think.auth import require_auth
from opal_think.config import ThinkConfig
from opal_think.deps import RuntimeState
from opal_think.errors import OpalHTTPException
from opal_think.opal_context import OpalContextMiddleware, get_opal_context
from opal_think.routers import health

logger = logging.getLogger("opal_think.app")


def _opal_error(request: Request, status: int, code: str, message: str, details: object = None) -> JSONResponse:
    ctx = get_opal_context(request)
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": jsonable_encoder(details),
                "request_id": ctx.request_id,
                "transaction_id": ctx.transaction_id,
                "session_id": ctx.session_id,
                "causation_id": ctx.causation_id,
            }
        },
    )


async def _opal_http_exception_handler(request: Request, exc: OpalHTTPException) -> JSONResponse:
    return _opal_error(request, exc.status_code, exc.code, exc.message, exc.details)


async def _request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return _opal_error(request, 422, "validation_error", "request validation failed", exc.errors())


_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    412: "precondition_failed",
    413: "payload_too_large",
    428: "precondition_required",
    429: "rate_limited",
    500: "internal",
    503: "unavailable",
}


async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Stray HTTPExceptions (starlette 404/405) also emit the OpalError envelope."""
    return _opal_error(
        request,
        exc.status_code,
        _HTTP_CODES.get(exc.status_code, "error"),
        str(exc.detail) if exc.detail else "request failed",
        None,
    )


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error processing %s %s", request.method, request.url.path)
    return _opal_error(request, 500, "internal", "internal server error", None)


def create_app(config: ThinkConfig | None = None) -> FastAPI:
    config = config or ThinkConfig()
    config.validate()

    app = FastAPI(title="OPAL Think API", version=API_VERSION)
    app.state.config = config
    app.state.runtime = RuntimeState()

    app.add_middleware(OpalContextMiddleware)
    app.add_exception_handler(OpalHTTPException, _opal_http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _request_validation_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, _http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled_exception_handler)

    app.include_router(health.router, dependencies=[Depends(require_auth)])

    async def _not_found() -> None:
        raise OpalHTTPException(404, "not_found", "resource does not exist")

    app.add_api_route(
        "/{full_path:path}",
        _not_found,
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
        include_in_schema=False,
    )

    return app
