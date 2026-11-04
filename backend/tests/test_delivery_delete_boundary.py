"""Deletion must revalidate delivery, identity and consumers at the unlink boundary."""
import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from delivery import DELIVERY_SCHEMA, enqueue, perform_job_cleanup, utc_now


@pytest.fixture
def delivered(tmp_path):
    db = tmp_path / "orders.db"
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    source = uploads / "1_photo.jpg"
    source.write_bytes(b"original retained photo")
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, payment_status TEXT)")
        conn.execute("INSERT INTO orders VALUES (1,'paid')")
        conn.executescript(DELIVERY_SCHEMA)
        job = enqueue(conn, order_id=1, kind="file", local_path=str(source),
                      remote_path="orders/1/photo.jpg", size=source.stat().st_size,
                      sha256=hashlib.sha256(source.read_bytes()).hexdigest())
        conn.execute("UPDATE delivery_jobs SET state='delivered', delivered_at=? WHERE id=?",
                     (utc_now(), job))
    return db, uploads, source, job


@pytest.mark.parametrize("change", ["same_size_replacement", "new_consumer", "delivery_revoked"])
def test_changes_after_initial_checks_never_delete_source(delivered, change):
    db, uploads, source, job = delivered

    def change_at_boundary(*_):
        if change == "same_size_replacement":
            source.write_bytes(b"X" * source.stat().st_size)
        else:
            with sqlite3.connect(db) as conn:
                if change == "new_consumer":
                    enqueue(conn, order_id=1, kind="file", local_path=str(source),
                            remote_path="orders/1/another-destination.jpg",
                            size=source.stat().st_size,
                            sha256=hashlib.sha256(source.read_bytes()).hexdigest())
                else:
                    conn.execute("UPDATE delivery_jobs SET state='failed' WHERE id=?", (job,))

    assert not perform_job_cleanup(db, job, uploads_dir=uploads, pre_unlink_hook=change_at_boundary)
    assert source.is_file(), "The last retained copy must survive a boundary race"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT local_deleted_at FROM delivery_jobs WHERE id=?", (job,)).fetchone()[0] is None


@pytest.mark.parametrize("column", ["content_sha256", "content_size"])
def test_missing_delivery_identity_never_authorizes_cleanup(delivered, column):
    db, uploads, source, job = delivered
    with sqlite3.connect(db) as conn:
        conn.execute(f"UPDATE delivery_jobs SET {column}=NULL WHERE id=?", (job,))
    assert not perform_job_cleanup(db, job, uploads_dir=uploads)
    assert source.read_bytes() == b"original retained photo"
