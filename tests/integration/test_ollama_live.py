"""Live Ollama smoke tests — opt-in via OPAL_THINK_LIVE_OLLAMA + OPAL_THINK_LIVE_MODEL.

Not part of ordinary CI: requires a reachable Ollama with the named model pulled.
Example: OPAL_THINK_LIVE_OLLAMA=http://192.168.1.88:11434 OPAL_THINK_LIVE_MODEL=smollm:360m pytest tests/integration
"""

from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

from opal_think.app import create_app
from opal_think.config import ThinkConfig
from opal_think.providers.ollama import OllamaAdapter

BASE = os.environ.get("OPAL_THINK_LIVE_OLLAMA")
MODEL = os.environ.get("OPAL_THINK_LIVE_MODEL", "smollm:360m")
pytestmark = pytest.mark.skipif(not BASE, reason="OPAL_THINK_LIVE_OLLAMA not set")


async def test_live_adapter_discovery() -> None:
    adapter = OllamaAdapter("ollama", BASE)
    models = await adapter.list_models()
    assert models, "no models discovered on live ollama"
    assert any(m.name == MODEL for m in models), f"{MODEL} not pulled"


async def test_live_adapter_generate() -> None:
    adapter = OllamaAdapter("ollama", BASE)
    result = await adapter.generate(MODEL, [{"role": "user", "content": "Reply with exactly: ok"}])
    assert result.text


def test_live_task_end_to_end(tmp_path) -> None:
    app = create_app(ThinkConfig(db_path=tmp_path / "t.db"))
    app.state.settings.store.set_many({"providers.ollama.base_url": BASE})
    with TestClient(app) as client:
        assert client.get("/api/v1/health/ready").status_code == 200
        created = client.post(
            "/api/v1/tasks/", json={"prompt": "Reply with exactly one word: pong", "model_id": f"ollama:{MODEL}"}
        )
        assert created.status_code == 202
        task_id = created.json()["id"]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            task = client.get(f"/api/v1/tasks/{task_id}").json()
            if task["status"] in {"completed", "failed", "cancelled", "timed_out"}:
                break
            time.sleep(1)
        assert task["status"] == "completed" and task["answer"]
