"""Regression tests for the two CRITICAL data-loss findings (C-01, C-02)."""
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import delivery  # noqa: E402
import retention  # noqa: E402

NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "orders.db"
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, payment_status TEXT NOT NULL)")
        conn.executescript(delivery.DELIVERY_SCHEMA)
    return db, uploads


def add(db, uploads, age_hours=73):
    path = uploads / "1_source.jpg"
    path.write_bytes(b"paid customer source")
    job = uuid.uuid4().hex
    created = (NOW - timedelta(hours=age_hours)).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT OR IGNORE INTO orders VALUES (1, 'unpaid')")
        conn.execute(
            "INSERT INTO delivery_jobs (id, order_id, kind, local_path, remote_path, state,"
            " next_attempt_at, created_at) VALUES (?, 1, 'file', ?, ?, 'pending', ?, ?)",
            (job, str(path), f"r/{job}", created, created),
        )
    return path, job


def actions(db):
    with sqlite3.connect(db) as conn:
        return [row[0] for row in conn.execute("SELECT action FROM retention_audit ORDER BY id")]


def set_paid(db):
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE orders SET payment_status='paid' WHERE id=1")


def claims(uploads):
    return list((uploads / ".quarantine").glob("*.claim"))


def test_c01_failed_restore_keeps_the_only_copy(store, monkeypatch):
    """C-01: a paid source must survive a failed restore instead of being destroyed."""
    db, uploads = store
    path, _ = add(db, uploads)
    original_unlink = Path.unlink

    def pay_then_unlink(self, *args, **kwargs):
        if self == path:
            set_paid(db)
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", pay_then_unlink)
    monkeypatch.setattr(retention, "_restore", lambda *a, **k: False)

    result = retention.purge_expired(db, uploads, now=NOW)

    assert result["recovery_required"] == 1
    assert "recovery_required" in actions(db)
    surviving = claims(uploads)
    assert surviving, "the quarantined copy was destroyed after a failed restore"
    assert surviving[0].read_bytes() == b"paid customer source"


def test_c01_restart_with_paid_order_keeps_quarantine(store, monkeypatch):
    """C-01 (restart path): an interrupted deletion of a paid order must stay recoverable."""
    db, uploads = store
    path, job = add(db, uploads)
    original_record = retention._record

    def crash_before_outcome(conn, job_id, order_id, action, **kwargs):
        if action in ("deleted", "restored_after_payment"):
            raise RuntimeError("crash after unlink")
        return original_record(conn, job_id, order_id, action, **kwargs)

    monkeypatch.setattr(retention, "_record", crash_before_outcome)
    with pytest.raises(RuntimeError):
        retention.purge_expired(db, uploads, now=NOW)
    monkeypatch.setattr(retention, "_record", original_record)
    set_paid(db)
    monkeypatch.setattr(retention, "_restore", lambda *a, **k: False)

    retention.purge_expired(db, uploads, now=NOW)

    assert claims(uploads), "restart discarded the paid quarantine"
    assert "recovery_required" in actions(db)


def test_c02_payment_after_deletion_restores_from_quarantine(store):
    """C-02: a payment committed after the deletion still finds a recoverable copy."""
    db, uploads = store
    path, _ = add(db, uploads)

    first = retention.purge_expired(db, uploads, now=NOW)
    assert first["deleted"] == 1
    assert not path.exists()
    assert claims(uploads), "the quarantine was removed in the same run"

    set_paid(db)
    released = retention.purge_quarantines(db, uploads, now=NOW + timedelta(minutes=5), grace_hours=0)

    assert released["restored"] == 1
    assert path.exists()
    assert path.read_bytes() == b"paid customer source"
    assert not claims(uploads)


def test_c02_unpaid_quarantine_is_released(store):
    db, uploads = store
    path, _ = add(db, uploads)
    retention.purge_expired(db, uploads, now=NOW)

    released = retention.purge_quarantines(db, uploads, now=NOW + timedelta(minutes=5), grace_hours=0)

    assert released["released"] == 1
    assert not claims(uploads)
    assert not path.exists()


def test_c02_quarantine_is_kept_within_the_grace_window(store):
    db, uploads = store
    add(db, uploads)
    retention.purge_expired(db, uploads, now=NOW)

    released = retention.purge_quarantines(db, uploads, now=NOW + timedelta(minutes=5), grace_hours=24)

    assert released["released"] == 0 and released["restored"] == 0
    assert claims(uploads)
