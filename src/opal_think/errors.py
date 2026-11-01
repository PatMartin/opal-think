"""OpalError envelope helpers and the service-level error type.

Every error response uses the nested ``OpalError`` shape from the contract:

    {"error": {"code", "message", "details"?, "request_id", "transaction_id",
               "session_id", "causation_id"}}

Context headers are applied by :class:`OpalContextMiddleware`, so handlers only build the body.
"""

from __future__ import annotations


class OpalHTTPException(Exception):
    """Service-level error rendered as an OpalError envelope."""

    def __init__(self, status_code: int, code: str, message: str, details: object = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
