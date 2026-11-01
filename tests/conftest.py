"""Shared fixtures: an in-process app over the real HTTP stack (starlette TestClient)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from opal_think.app import create_app
from opal_think.config import AuthConfig, ThinkConfig


@pytest.fixture
def config(tmp_path) -> ThinkConfig:
    return ThinkConfig(db_path=tmp_path / "think.db")


@pytest.fixture
def client(config: ThinkConfig) -> Iterator[TestClient]:
    with TestClient(create_app(config)) as test_client:
        yield test_client


@pytest.fixture
def authed_client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    """App with bearer auth enabled and the token env var populated."""
    monkeypatch.setenv("OPAL_THINK_API_TOKEN", "test-token-123")
    config = ThinkConfig(db_path=tmp_path / "think.db", auth=AuthConfig(enabled=True))
    with TestClient(create_app(config)) as test_client:
        yield test_client
