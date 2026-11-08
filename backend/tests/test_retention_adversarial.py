"""Independent safety regressions for the PR #31 retention boundary.

All paths and databases are pytest temporary fixtures; no live data is accessed.
"""
import os
import sqlite3
import sys
import threading
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


def add_job(db, path, order_id=1, age_hours=73, state="pending"):
    job_id = uuid.uuid4().hex
    created = (NOW - timedelta(hours=age_hours)).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT OR IGNORE INTO orders VALUES (?, 'unpaid')", (order_id,))
        conn.execute(
            "INSERT INTO delivery_jobs (id, order_id, kind, local_path, remote_path, "
            "state, next_attempt_at, created_at) VALUES (?, ?, 'file', ?, ?, ?, ?, ?)",
            (job_id, order_id, str(path), f"remote/{job_id}", state, created, created),
        )
    return job_id


def actions(db):
    with sqlite3.connect(db) as conn:
        return [row[0] for row in conn.execute("SELECT action FROM retention_audit ORDER BY id")]


def test_payment_committed_after_intent_before_unlink_preserves_source(store, monkeypatch):
    db, uploads = store
    path = uploads / "1_source.jpg"
    path.write_bytes(b"paid customer source")
    add_job(db, path)
    original_unlink = Path.unlink

    def pay_then_unlink(self, *args, **kwargs):
        if self == path:
            with sqlite3.connect(db, timeout=1) as conn:
                conn.execute("UPDATE orders SET payment_status='paid' WHERE id=1")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", pay_then_unlink)
    retention.purge_expired(db, uploads, now=NOW)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT payment_status FROM orders WHERE id=1").fetchone()[0] == "paid"
    assert path.exists(), "Retention deleted the source after payment was committed"


def test_pending_payment_is_not_purged_while_confirmation_can_still_arrive(store):
    db, uploads = store
    path = uploads / "1_source.jpg"
    path.write_bytes(b"customer source")
    add_job(db, path)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE orders SET payment_status='pending' WHERE id=1")
    retention.purge_expired(db, uploads, now=NOW)
    # main.mark_order_paid accepts pending -> paid without checking the source.
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE orders SET payment_status='paid' WHERE id=1 AND payment_status='pending'")
        assert conn.total_changes == 1
    assert path.exists(), "Payment completed for an order whose pending source was purged"


def test_crash_after_unlink_recovers_deleted_audit(store, monkeypatch):
    db, uploads = store
    path = uploads / "1_source.jpg"
    path.write_bytes(b"customer source")
    add_job(db, path)
    original_record = retention._record

    def crash_before_final_status(conn, job_id, order_id, action, **kwargs):
        if action == "deleted":
            raise RuntimeError("simulated process crash after unlink")
        return original_record(conn, job_id, order_id, action, **kwargs)

    monkeypatch.setattr(retention, "_record", crash_before_final_status)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        retention.purge_expired(db, uploads, now=NOW)
    assert not path.exists()
    assert actions(db) == ["deleting"]

    monkeypatch.setattr(retention, "_record", original_record)
    retention.purge_expired(db, uploads, now=NOW)
    assert actions(db) == ["deleting", "deleted"], "Restart left deletion permanently unfinalized"


def test_crash_after_intent_before_unlink_retries_unpaid_source(store, monkeypatch):
    db, uploads = store
    path = uploads / "1_source.jpg"
    path.write_bytes(b"customer source")
    add_job(db, path)
    original_unlink = Path.unlink

    def crash_before_unlink(self, *args, **kwargs):
        if self == path:
            raise RuntimeError("simulated crash before unlink")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", crash_before_unlink)
    with pytest.raises(RuntimeError, match="simulated crash"):
        retention.purge_expired(db, uploads, now=NOW)
    assert path.exists()
    assert actions(db) == ["deleting"]

    monkeypatch.setattr(Path, "unlink", original_unlink)
    retention.purge_expired(db, uploads, now=NOW)
    assert not path.exists()
    assert actions(db) == ["deleting", "deleted"]


def test_parent_symlink_replacement_cannot_delete_outside_uploads(store, tmp_path, monkeypatch):
    db, uploads = store
    parent = uploads / "folder"
    parent.mkdir()
    path = parent / "1_source.jpg"
    path.write_bytes(b"uploaded source")
    add_job(db, path)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / path.name
    outside_file.write_bytes(b"unrelated protected file")
    probe = tmp_path / "symlink_probe"
    try:
        os.symlink(outside, probe, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Directory symlinks unavailable: {exc}")
    probe.unlink()
    original_unlink = Path.unlink

    def replace_parent_then_unlink(self, *args, **kwargs):
        if self == path:
            parent.rename(uploads / "old_folder")
            os.symlink(outside, parent, target_is_directory=True)
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", replace_parent_then_unlink)
    retention.purge_expired(db, uploads, now=NOW)
    assert outside_file.exists(), "Unlink followed a swapped parent symlink outside uploads"


def test_missing_order_does_not_authorize_deletion(store):
    db, uploads = store
    path = uploads / "1_source.jpg"
    path.write_bytes(b"customer source")
    add_job(db, path)
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM orders WHERE id=1")
    retention.purge_expired(db, uploads, now=NOW)
    assert path.exists(), "Missing order was treated as unpaid"


def test_dry_run_does_not_create_database_objects(store):
    db, uploads = store
    path = uploads / "1_source.jpg"
    path.write_bytes(b"customer source")
    add_job(db, path)
    with sqlite3.connect(db) as conn:
        before = conn.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall()
    retention.purge_expired(db, uploads, now=NOW, dry_run=True)
    with sqlite3.connect(db) as conn:
        after = conn.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall()
    assert after == before, "Dry run changed the database schema"


def test_source_substitution_before_unlink_preserves_new_bytes(store, monkeypatch):
    db, uploads = store
    path = uploads / "1_source.jpg"
    path.write_bytes(b"original source")
    add_job(db, path)
    original_unlink = Path.unlink

    def substitute_then_unlink(self, *args, **kwargs):
        if self == path:
            self.write_bytes(b"replacement customer data")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", substitute_then_unlink)
    retention.purge_expired(db, uploads, now=NOW)
    assert path.exists(), "Purged a replacement file without identity check"
    assert path.read_bytes() == b"replacement customer data", "Purged a replacement file without identity check"


def test_two_workers_do_not_report_spurious_unlink_failure(store, monkeypatch):
    db, uploads = store
    path = uploads / "1_source.jpg"
    path.write_bytes(b"customer source")
    add_job(db, path)
    original_unlink = Path.unlink
    both_at_unlink = threading.Barrier(2, timeout=5)
    results = []
    exceptions = []

    def concurrent_unlink(self, *args, **kwargs):
        if self == path:
            both_at_unlink.wait()
        return original_unlink(self, *args, **kwargs)

    def run():
        try:
            results.append(retention.purge_expired(db, uploads, now=NOW))
        except Exception as exc:
            exceptions.append(exc)

    monkeypatch.setattr(Path, "unlink", concurrent_unlink)
    workers = [threading.Thread(target=run) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=8)
    assert not any(worker.is_alive() for worker in workers)
    assert not exceptions
    assert sum(result["deleted"] for result in results) == 1
    assert all(not result["errors"] for result in results), "Duplicate worker reported normal race as failure"


def test_naive_legacy_timestamp_exactly_at_cutoff_is_not_expired(store):
    db, uploads = store
    path = uploads / "1_source.jpg"
    path.write_bytes(b"customer source")
    job_id = add_job(db, path)
    # A naive historical/imported job timestamp is accepted by the TEXT schema.
    exact_cutoff = (NOW - timedelta(hours=72)).replace(tzinfo=None).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE delivery_jobs SET created_at=? WHERE id=?", (exact_cutoff, job_id))
    retention.purge_expired(db, uploads, now=NOW)
    assert path.exists(), "An exactly 72-hour-old production-format timestamp was deleted"


def test_sqlite_writer_lock_fails_closed(store, monkeypatch):
    db, uploads = store
    path = uploads / "1_source.jpg"
    path.write_bytes(b"customer source")
    add_job(db, path)
    lock_holder = sqlite3.connect(db)
    lock_holder.execute("BEGIN EXCLUSIVE")
    original_connect = sqlite3.connect

    def short_timeout_connect(path, timeout=10, **kwargs):
        return original_connect(path, timeout=0.1, **kwargs)

    monkeypatch.setattr(retention.sqlite3, "connect", short_timeout_connect)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            retention.purge_expired(db, uploads, now=NOW)
        assert path.exists(), "Retention deleted despite a blocking SQLite writer"
    finally:
        lock_holder.rollback()
        lock_holder.close()


def test_parent_traversal_is_refused(store):
    db, uploads = store
    subdir = uploads / "subdir"
    subdir.mkdir()
    path = uploads / "1_source.jpg"
    path.write_bytes(b"customer source")
    add_job(db, subdir / ".." / path.name)
    result = retention.purge_expired(db, uploads, now=NOW)
    assert path.exists()
    assert result["skipped_unsafe"] == 1


def test_orphan_scan_preserves_unknown_order_file(store):
    db, uploads = store
    path = uploads / "42_customer_source.jpg"
    path.write_bytes(b"untracked customer source")
    old = (NOW - timedelta(hours=100)).timestamp()
    os.utime(path, (old, old))
    retention.purge_expired(db, uploads, now=NOW, orphan_scan=True)
    assert path.exists(), "Optional orphan scan deleted an untracked customer file"
