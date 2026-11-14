"""Worker-level rotation policy regressions: real dummy backups, no real data."""
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

import backup_rotation
import delivery
import retention_worker

NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)


@pytest.fixture
def cycle(tmp_path, monkeypatch):
    db = tmp_path / "orders.db"
    uploads = tmp_path / "uploads"
    backups = tmp_path / "backups"
    uploads.mkdir()
    backups.mkdir()
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("CREATE TABLE orders(id INTEGER PRIMARY KEY,payment_status TEXT)")
        conn.executescript(delivery.DELIVERY_SCHEMA)
    monkeypatch.setenv("BACKUP_ROTATION_ENABLED", "1")
    monkeypatch.setenv("BACKUP_DIR", str(backups))
    monkeypatch.setenv("BACKUP_RETENTION_DAYS", "30")
    monkeypatch.setenv("BACKUP_KEEP_MIN", "0")
    monkeypatch.setenv("BACKUP_MAX_AGE_DAYS", "30")
    monkeypatch.setenv("LOG_ROTATION_ENABLED", "0")
    return db, uploads, backups


def backup(backups, name, age):
    path = backups / name
    path.write_bytes(b"synthetic backup only")
    stamp = (NOW - timedelta(days=age)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def run(cycle, **kwargs):
    db, uploads, backups = cycle
    return retention_worker.run_retention_cycle(db, uploads, now=NOW, **kwargs)["backup_rotation"]


def test_nondefault_window_preserves_backups_inside_configured_retention(cycle):
    files = [backup(cycle[2], f"2026010{i}T000000Z", age)
             for i, age in enumerate((5, 6, 7, 8), 1)]
    policy = run(cycle)
    assert policy["retention_days"] == 30 and policy["keep_min"] == 0
    assert policy["max_age_days"] == 30 and not policy["removed"]
    assert all(path.exists() for path in files)


def test_hard_maximum_is_applied_even_when_keep_min_exceeds_backup_count(cycle, monkeypatch):
    monkeypatch.setenv("BACKUP_RETENTION_DAYS", "7")
    monkeypatch.setenv("BACKUP_KEEP_MIN", "5")
    old = backup(cycle[2], "20260101T000000Z", 40)
    recent = backup(cycle[2], "20260102T000000Z", 10)
    assert len(run(cycle)["removed"]) == 1
    assert not old.exists() and recent.exists()


def test_keep_min_and_retention_values_both_reach_worker(cycle, monkeypatch):
    monkeypatch.setenv("BACKUP_RETENTION_DAYS", "1")
    monkeypatch.setenv("BACKUP_KEEP_MIN", "1")
    newer = backup(cycle[2], "20260101T000000Z", 4)
    older = backup(cycle[2], "20260102T000000Z", 5)
    policy = run(cycle)
    assert policy["retention_days"] == 1 and policy["keep_min"] == 1
    assert newer.exists() and not older.exists()


@pytest.mark.parametrize("key,value", [
    ("BACKUP_RETENTION_DAYS", "0"), ("BACKUP_KEEP_MIN", "-1"),
    ("BACKUP_MAX_AGE_DAYS", "2"), ("BACKUP_RETENTION_DAYS", "bad"),
])
def test_invalid_policy_fails_before_any_lifecycle_mutation(cycle, monkeypatch, key, value):
    path = backup(cycle[2], "20260101T000000Z", 90)
    monkeypatch.setenv(key, value)
    monkeypatch.setattr(retention_worker.retention, "purge_expired",
                        lambda *a, **kw: pytest.fail("purge ran with invalid rotation config"))
    with pytest.raises(ValueError):
        run(cycle)
    assert path.exists()
    with closing(sqlite3.connect(cycle[0])) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='retention_telemetry'").fetchone()


def test_nondefault_dry_run_plans_without_deleting_backups(cycle):
    path = backup(cycle[2], "20260101T000000Z", 40)
    policy = run(cycle, dry_run=True)
    assert len(policy["removed"]) == 1 and path.exists()
    assert not (cycle[2] / "rotation.jsonl").exists()


def test_startup_and_worker_share_policy_loader(cycle, monkeypatch):
    import main
    captured = []
    original = backup_rotation.configured_policy

    def load():
        policy = original()
        captured.append(policy)
        return policy

    monkeypatch.setenv("LOG_ROTATION_VERIFICATION_ENABLED", "0")
    monkeypatch.setattr(backup_rotation, "configured_policy", load)
    main.validate_rotation_safety()
    run(cycle)
    assert len(captured) == 2 and captured[0] == captured[1]
