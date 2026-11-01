"""Bearer-token REST authentication, enabled via ``ThinkConfig.auth``.

Per suite policy auth may be disabled only on loopback binds; the non-loopback requirement is
enforced by :meth:`ThinkConfig.validate` at startup, not here. Every failure uses the OpalError
envelope (401 missing/invalid token, 503 when enabled but the token env var is unset).
"""

from __future__ import annotations

import hmac
import os

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from opal_think.errors import OpalHTTPException

_bearer_scheme = HTTPBearer(auto_error=False)


def require_auth(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme)) -> None:
    auth = request.app.state.config.auth
    if not auth.enabled:
        return
    expected = os.environ.get(auth.token_env)
    if not expected:
        raise OpalHTTPException(503, "auth_misconfigured", "REST auth is enabled but token_env is unset")
    if credentials is None or not hmac.compare_digest(credentials.credentials, expected):
        raise OpalHTTPException(401, "unauthorized", "missing or invalid bearer token")
