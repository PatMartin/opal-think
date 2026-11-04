"""Unit tests: EVENT action metadata flows into provider tool specs."""

from __future__ import annotations

from opal_think.engine import TaskEngine


def _spec(action: dict) -> dict:
    return TaskEngine._tool_spec(action)["function"]


def test_metadata_action_uses_description_and_input_schema() -> None:
    action = {
        "id": "announce_person",
        "type": "shell",
        "enabled": True,
        "description": "Announce a person detected by a camera.",
        "input_schema": {"type": "object", "properties": {"camera": {"type": "string"}}, "required": ["camera"]},
        "keywords": ["camera", "announce"],
        "suggested_prompts": ["tell me who is at the door"],
    }
    fn = _spec(action)
    assert "Announce a person" in fn["description"]
    assert "keywords: camera, announce" in fn["description"]
    assert "example uses:" in fn["description"]
    params = fn["parameters"]["properties"]
    assert params["data"] == action["input_schema"]  # nested schema mapped to event.data
    assert params["topic"]["type"] == "string"
    assert fn["parameters"]["required"] == ["topic", "data"]


def test_legacy_action_fallback() -> None:
    fn = _spec({"id": "old_action", "type": "shell", "enabled": True})
    assert "old_action" in fn["description"]
    assert fn["parameters"]["properties"]["data"] == {"type": "object", "description": "EVENT event data payload"}


def test_malformed_metadata_is_safe() -> None:
    fn = _spec({
        "id": "x", "type": "shell",
        "input_schema": "not-a-dict",
        "description": "ok\x00hidden\x07text",
        "keywords": "not-a-list",
        "suggested_prompts": ["a" * 5000, 42],
    })
    params = fn["parameters"]["properties"]
    assert params["data"]["type"] == "object" and "not-a-dict" not in str(params["data"])
    assert "\x00" not in fn["description"] and "\x07" not in fn["description"]
    assert "not-a-list" not in fn["description"]
    assert len(fn["description"]) < 1500  # bounded hints
