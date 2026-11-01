"""Unit tests for the settings registry and SQLite override store."""

from __future__ import annotations

from opal_think.settings_registry import FIXED_SETTINGS, SettingsRegistry, SettingsStore, lookup


def _registry(tmp_path) -> SettingsRegistry:
    return SettingsRegistry(SettingsStore(tmp_path / "t.db"))


def test_lookup_fixed_and_dynamic() -> None:
    assert lookup("task.max_concurrent") is FIXED_SETTINGS["task.max_concurrent"]
    assert lookup("providers.gpu_rig.enabled") is not None
    assert lookup("providers.gpu_rig.bogus") is None
    assert lookup("unknown.key") is None


def test_value_validation() -> None:
    assert lookup("task.max_concurrent").check(4) is None
    assert lookup("task.max_concurrent").check("4") is not None
    assert lookup("providers.x.enabled").check(True) is None
    assert lookup("providers.x.enabled").check("yes") is not None
    assert lookup("event.credential_ref").check("env:MY_TOKEN") is None
    assert lookup("event.credential_ref").check("hunter2") is not None


def test_revisions_advance(tmp_path) -> None:
    registry = _registry(tmp_path)
    assert registry.store.global_revision() == 0
    registry.store.set_many({"task.max_concurrent": 3})
    assert registry.store.global_revision() == 1
    assert registry.get("task.max_concurrent")["revision"] == 1
    registry.store.set_many({"task.retention_days": 9})
    # unrelated key revision unchanged: per-key optimistic concurrency
    assert registry.get("task.max_concurrent")["revision"] == 1
    assert registry.get("task.retention_days")["revision"] == 2
    registry.store.reset("task.max_concurrent")
    assert registry.get("task.max_concurrent")["is_default"] is True


def test_snapshot_sorted_and_pending(tmp_path) -> None:
    registry = _registry(tmp_path)
    items, revision, pending = registry.snapshot()
    assert revision == 0 and pending == []
    assert [i["key"] for i in items] == sorted(i["key"] for i in items)
