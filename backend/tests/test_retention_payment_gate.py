"""Late payment callbacks must never leave a paid order that looks processable.

Retention removes the source of an order that stayed unpaid past its window. If the
provider later confirms the payment, the money is real: the order must stay paid and the
missing source must be visible to the operator instead of failing silently.
"""
import os
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
main = pytest.importorskip("main")

NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.db")
    monkeypatch.setenv("PHOTODOC_ALLOW_FRESH_DB", "1")
    monkeypatch.setenv("PHOTODOC_STAGING", "1")
    main.init_db()
    return main.DB_PATH


def add_order(conn, order_id=1, status="unpaid"):
    conn.execute(
        "INSERT INTO orders (id, name, phone, email, filename, status, payment_status,"
        " order_amount, payment_amount, order_token_hash, created_at)"
        " VALUES (?, 'T', '000', 't@example.invalid', 'x.jpg', 'awaiting_payment', ?, 0, NULL, 'h', ?)",
        (order_id, status, NOW.isoformat()),
    )


def add_job(conn, order_id, path, state="pending"):
    job_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO delivery_jobs (id, order_id, kind, local_path, remote_path, state,"
        " next_attempt_at, created_at) VALUES (?, ?, 'file', ?, ?, ?, ?, ?)",
        (job_id, order_id, str(path), f"PhotoDoc_AI/{order_id}_x.jpg", state,
         NOW.isoformat(), (NOW - timedelta(hours=90)).isoformat()),
    )
    return job_id


def last_error(db_path, job_id):
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT last_error FROM delivery_jobs WHERE id=?", (job_id,)).fetchone()
        return row[0] if row else None


def payment_status(db_path, order_id=1):
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT payment_status FROM orders WHERE id=?", (order_id,)).fetchone()[0]


def test_late_payment_after_source_deletion_is_flagged(db, tmp_path):
    missing = tmp_path / "uploads" / "1_source.jpg"  # never created: retention removed it
    with sqlite3.connect(db) as conn:
        add_order(conn)
        job_id = add_job(conn, 1, missing)

    assert main.mark_order_paid(1, "20.00") is True

    assert payment_status(db) == "paid"
    assert last_error(db, job_id) == "SourceMissingAtPayment"


def test_payment_with_existing_source_is_not_flagged(db, tmp_path):
    source = tmp_path / "uploads" / "1_source.jpg"
    source.parent.mkdir()
    source.write_bytes(b"present")
    with sqlite3.connect(db) as conn:
        add_order(conn)
        job_id = add_job(conn, 1, source)

    assert main.mark_order_paid(1, "20.00") is True

    assert payment_status(db) == "paid"
    assert last_error(db, job_id) is None


def test_delivered_job_without_local_file_is_not_flagged(db, tmp_path):
    removed = tmp_path / "uploads" / "1_source.jpg"
    with sqlite3.connect(db) as conn:
        add_order(conn)
        job_id = add_job(conn, 1, removed, state="delivered")

    main.mark_order_paid(1, "20.00")

    assert last_error(db, job_id) is None
