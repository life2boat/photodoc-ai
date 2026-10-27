"""
PhotoDoc AI — R3.1 Historical Cleanup Migration Regression Tests.

Validates:
1. Historical delivered job whose local file still exists: schedules verified cleanup (pending, local_deleted_at=NULL).
2. Historical delivered job whose local file is absent: reconciles state (completed, local_deleted_at=NULL) without inventing timestamps.
3. Failed migration transaction cleanly rolls back and allows a safe retry.
4. Re-running an already completed migration is an idempotent no-op.
5. Full preservation of existing orders, payments, tokens, and delivery history.
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from db_contract import APPLICATION_ID, EXPECTED_ORDERS_COLUMNS  # noqa: E402
from delivery import (  # noqa: E402
    DELIVERY_SCHEMA,
    get_uploads_dir,
    perform_job_cleanup,
    process_due_cleanups,
    set_uploads_dir,
    utc_now,
)
from migrations.reconcile_delivery_schema import reconcile  # noqa: E402


def setup_legacy_p1_db(db_path: Path) -> None:
    """
    Creates a P1 database with pre-cleanup delivery_jobs schema
    (as existed before PR #12 added cleanup columns), strictly adhering to P0/P1 DB contract.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"PRAGMA application_id = {APPLICATION_ID};")
        conn.execute("PRAGMA journal_mode = WAL;")
        cols = list(EXPECTED_ORDERS_COLUMNS)
        if "order_token_hash" not in cols:
            cols.append("order_token_hash")
        cols_def = ", ".join(f"{col} TEXT" if col != "id" else "id INTEGER PRIMARY KEY AUTOINCREMENT" for col in cols)
        conn.execute(f"CREATE TABLE orders ({cols_def})")
        # Pre-cleanup delivery_jobs schema without cleanup columns
        conn.execute("""
            CREATE TABLE delivery_jobs (
                id TEXT PRIMARY KEY,
                order_id INTEGER NOT NULL REFERENCES orders(id),
                kind TEXT NOT NULL CHECK(kind IN ('file', 'info', 'admin_email', 'paid_email')),
                local_path TEXT,
                original_name TEXT,
                remote_path TEXT,
                content_sha256 TEXT,
                content_size INTEGER,
                state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending', 'processing', 'delivered', 'failed')),
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                lease_until TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                delivered_at TEXT,
                UNIQUE(order_id, kind, remote_path)
            );
        """)
        conn.execute("CREATE INDEX idx_delivery_due ON delivery_jobs(state, next_attempt_at, lease_until);")
        conn.execute("CREATE UNIQUE INDEX idx_paid_email_once ON delivery_jobs(order_id) WHERE kind = 'paid_email';")
        conn.execute("CREATE UNIQUE INDEX idx_admin_email_once ON delivery_jobs(order_id) WHERE kind = 'admin_email';")
        conn.commit()
    finally:
        conn.close()


def test_migration_backfill_delivered_job_with_existing_file_schedules_cleanup(tmp_path):
    """
    1. Historical delivered job whose local file STILL exists on disk:
    Migration must NOT mark it as completed or invent local_deleted_at.
    It must set cleanup_state='pending', local_deleted_at=NULL so the worker cleans it.
    """
    db_path = tmp_path / "orders.db"
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()
    set_uploads_dir(uploads_dir)

    setup_legacy_p1_db(db_path)

    photo_file = uploads_dir / "1_test_photo.jpg"
    photo_file.write_bytes(b"legacy-photo-bytes-for-delivery")
    photo_sha = hashlib.sha256(photo_file.read_bytes()).hexdigest()
    photo_size = len(photo_file.read_bytes())
    delivered_time = "2026-09-01T10:00:00+00:00"

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO orders (id, name, phone, payment_status, payment_amount, order_token_hash) "
            "VALUES (1, 'Historical Client', '+79991234567', 'paid', 300.0, 'hash1')"
        )
        conn.execute(
            "INSERT INTO delivery_jobs (id, order_id, kind, local_path, original_name, remote_path, "
            "content_sha256, content_size, state, attempts, next_attempt_at, created_at, delivered_at) "
            "VALUES ('job-legacy-1', 1, 'file', ?, 'test_photo.jpg', 'Remote/1_test_photo.jpg', "
            "?, ?, 'delivered', 1, ?, ?, ?)",
            (str(photo_file), photo_sha, photo_size, delivered_time, delivered_time, delivered_time),
        )

    # Run migration
    res = reconcile(db_path, apply=True, uploads_dir=uploads_dir)
    assert res["migration_required"] is True
    assert res["applied"] is True

    # Check backfilled state
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT state, cleanup_state, local_deleted_at, delivered_at FROM delivery_jobs WHERE id='job-legacy-1'"
        ).fetchone()
        assert row[0] == "delivered"
        assert row[1] == "pending", "Must be pending so cleanup worker can safely delete the file"
        assert row[2] is None, "Must NOT invent a deletion timestamp for existing file!"
        assert row[3] == delivered_time, "Delivered_at must be preserved!"

    assert photo_file.is_file(), "Migration itself must NEVER delete files!"

    # Now verify the cleanup worker cleans up the file
    cleaned = process_due_cleanups(db_path, uploads_dir=uploads_dir)
    assert cleaned == 1
    assert not photo_file.exists(), "File should now be cleaned up by worker"

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT cleanup_state, local_deleted_at FROM delivery_jobs WHERE id='job-legacy-1'"
        ).fetchone()
        assert row[0] == "completed"
        assert row[1] is not None, "Timestamp set upon actual deletion"


def test_migration_backfill_delivered_job_with_absent_file_reconciles_cleanly(tmp_path):
    """
    2. Historical delivered job whose local file is ABSENT from disk:
    Migration reconciles cleanup_state='completed' WITHOUT inventing local_deleted_at=delivered_at.
    """
    db_path = tmp_path / "orders.db"
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()
    set_uploads_dir(uploads_dir)

    setup_legacy_p1_db(db_path)

    delivered_time = "2026-08-15T08:30:00+00:00"
    absent_path = uploads_dir / "2_already_gone.jpg"
    assert not absent_path.exists()

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO orders (id, name, phone, payment_status, payment_amount, order_token_hash) "
            "VALUES (2, 'Gone Client', '+79998887766', 'paid', 300.0, 'hash2')"
        )
        conn.execute(
            "INSERT INTO delivery_jobs (id, order_id, kind, local_path, original_name, remote_path, "
            "content_sha256, content_size, state, attempts, next_attempt_at, created_at, delivered_at) "
            "VALUES ('job-legacy-2', 2, 'file', ?, 'gone.jpg', 'Remote/2_gone.jpg', "
            "'fakehash', 100, 'delivered', 1, ?, ?, ?)",
            (str(absent_path), delivered_time, delivered_time, delivered_time),
        )

    res = reconcile(db_path, apply=True, uploads_dir=uploads_dir)
    assert res["migration_required"] is True
    assert res["applied"] is True

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT state, cleanup_state, local_deleted_at, delivered_at FROM delivery_jobs WHERE id='job-legacy-2'"
        ).fetchone()
        assert row[0] == "delivered"
        assert row[1] == "completed", "Reconciled to completed because file is already absent"
        assert row[2] is None, "Must NOT invent delivered_at as local_deleted_at timestamp!"
        assert row[3] == delivered_time


def test_migration_transaction_rollback_and_safe_retry(tmp_path):
    """
    3. Failed migration transaction rolls back cleanly without corrupting DB,
    and a subsequent retry succeeds safely.
    """
    db_path = tmp_path / "orders.db"
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()

    setup_legacy_p1_db(db_path)

    # Inject failure during schema execution
    original_connect = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("Simulated disk error on commit")

    with patch("sqlite3.connect", side_effect=lambda *a, **kw: original_connect(db_path, timeout=10, factory=FailingConnection)):
        with pytest.raises(sqlite3.OperationalError, match="Simulated disk error"):
            reconcile(db_path, apply=True, uploads_dir=uploads_dir)

    # Verify database is intact and still valid SQLite
    with sqlite3.connect(db_path) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok"
        cols = {row[1] for row in conn.execute("PRAGMA table_info(delivery_jobs)")}
        assert "cleanup_state" not in cols, "Columns rolled back cleanly"

    # Retry migration cleanly
    retry_res = reconcile(db_path, apply=True, uploads_dir=uploads_dir)
    assert retry_res["migration_required"] is True
    assert retry_res["applied"] is True

    with sqlite3.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(delivery_jobs)")}
        assert "cleanup_state" in cols


def test_migration_idempotent_rerun(tmp_path):
    """
    4. Re-running an already completed migration is a safe no-op.
    """
    db_path = tmp_path / "orders.db"
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()

    setup_legacy_p1_db(db_path)

    first_res = reconcile(db_path, apply=True, uploads_dir=uploads_dir)
    assert first_res["migration_required"] is True
    assert first_res["applied"] is True

    second_res = reconcile(db_path, apply=True, uploads_dir=uploads_dir)
    assert second_res["migration_required"] is False
    assert second_res["applied"] is False


def test_migration_preserves_orders_payments_and_delivery_history(tmp_path):
    """
    5. Full preservation of orders, payments, tokens, and delivery history.
    """
    db_path = tmp_path / "orders.db"
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()

    setup_legacy_p1_db(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO orders (id, name, phone, comment, format, paper, crop, filename, "
            "created_at, payment_status, payment_amount, order_token_hash, paid_at) "
            "VALUES (10, 'Bob Builder', '+79990001122', 'Extra glossy please', '10x15', 'Glossy', 'no_crop', "
            "'sample.jpg', '2026-05-01T12:00:00+00:00', 'paid', 450.0, 'token_hash_abc', '2026-05-01T12:05:00+00:00')"
        )
        conn.execute(
            "INSERT INTO delivery_jobs (id, order_id, kind, local_path, original_name, remote_path, "
            "content_sha256, content_size, state, attempts, next_attempt_at, created_at, delivered_at) "
            "VALUES ('dj-10', 10, 'admin_email', NULL, NULL, 'email:admin', "
            "NULL, NULL, 'delivered', 1, '2026-05-01T12:06:00', '2026-05-01T12:05:00', '2026-05-01T12:06:00')"
        )

    reconcile(db_path, apply=True, uploads_dir=uploads_dir)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        order = conn.execute("SELECT * FROM orders WHERE id=10").fetchone()
        assert order["name"] == "Bob Builder"
        assert order["phone"] == "+79990001122"
        assert order["comment"] == "Extra glossy please"
        assert order["payment_status"] == "paid"
        assert float(order["payment_amount"]) == 450.0
        assert order["order_token_hash"] == "token_hash_abc"
        assert order["paid_at"] == "2026-05-01T12:05:00+00:00"

        job = conn.execute("SELECT * FROM delivery_jobs WHERE id='dj-10'").fetchone()
        assert job["kind"] == "admin_email"
        assert job["state"] == "delivered"
        assert job["cleanup_state"] == "not_applicable", "Email jobs must be marked not_applicable"
        assert job["delivered_at"] == "2026-05-01T12:06:00"
