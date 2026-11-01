"""Thin HTTPX client for the OPAL-THINK REST API.

All commands go through this client over HTTP; no server internals are imported.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_API_URL = "http://127.0.0.1:8080"


def api_url_from_env() -> str:
    return os.environ.get("OPAL_THINK_API_URL", DEFAULT_API_URL)


def token_from_env() -> str | None:
    return os.environ.get("OPAL_THINK_API_TOKEN")


class ApiCallError(Exception):
    """REST call failed; carries HTTP status and the OpalError body when present."""

    def __init__(self, status_code: int, message: str, body: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class ApiClient:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 30.0) -> None:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout)

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiCallError(0, f"cannot reach OPAL-THINK API: {exc}") from exc
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = None
            message = (
                body.get("error", {}).get("message", response.text)
                if isinstance(body, dict)
                else response.text or f"HTTP {response.status_code}"
            )
            raise ApiCallError(response.status_code, message, body)
        return response

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)
