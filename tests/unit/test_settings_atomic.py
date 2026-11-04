"""Unit tests: settings ETag precondition + mutation are one atomic transaction."""

from __future__ import annotations

import threading

from opal_think.settings_registry import RevisionConflict, SettingsStore


def test_concurrent_writers_same_etag_exactly_one_wins(tmp_path) -> None:
    """Two separate handles racing with the same collection ETag: one commits,
    the loser observes the bumped revision and gets RevisionConflict — no lost update."""
    db = tmp_path / "settings.db"
    store_a, store_b = SettingsStore(db), SettingsStore(db)
    results: list[tuple[str, str]] = []
    barrier = threading.Barrier(2)

    def writer(store: SettingsStore, value: int, tag: str) -> None:
        try:
            barrier.wait()
            store.set_many_checked({"task.max_concurrent": value}, expect_global=0)
            results.append(("ok", tag))
        except RevisionConflict:
            results.append(("conflict", tag))

    threads = [
        threading.Thread(target=writer, args=(store_a, 300, "a")),
        threading.Thread(target=writer, args=(store_b, 400, "b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert sorted(r[0] for r in results) == ["conflict", "ok"], results
    winner = next(tag for outcome, tag in results if outcome == "ok")
    assert store_a.get_override("task.max_concurrent") is not None
    # only the winner's value persisted — no lost update
    expected = 300 if winner == "a" else 400
    assert store_a.get_override("task.max_concurrent")[0] == expected


def test_key_revision_conflict_rejected(tmp_path) -> None:
    store = SettingsStore(tmp_path / "s.db")
    store.set_many_checked({"task.max_concurrent": 60})
    key_rev = store.get_override("task.max_concurrent")[1]
    try:
        store.set_many_checked({"task.max_concurrent": 90}, key="task.max_concurrent", expect_key=key_rev + 1)
        raise AssertionError("expected RevisionConflict")
    except RevisionConflict as exc:
        assert exc.expected == key_rev + 1 and exc.actual == key_rev


def test_delete_with_stale_key_etag_rejected(tmp_path) -> None:
    store = SettingsStore(tmp_path / "s.db")
    store.set_many_checked({"task.max_concurrent": 60})
    key_rev = store.get_override("task.max_concurrent")[1]
    store.set_many_checked({"task.max_concurrent": 90})  # another writer bumps the key rev
    try:
        store.reset_checked("task.max_concurrent", expect_key=key_rev)
        raise AssertionError("expected RevisionConflict")
    except RevisionConflict:
        pass
    # the override survived — no lost update
    assert store.get_override("task.max_concurrent") is not None
