"""P1 queue concurrency, lease fencing, and retry bounds tests."""
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
from delivery import claim_due, enqueue, finish, process_due, utc_now, DELIVERY_SCHEMA  # noqa: E402
from db_contract import APPLICATION_ID  # noqa: E402


@pytest.fixture
def test_db(tmp_path):
    db = tmp_path / "orders.db"
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
    conn.execute("""
    CREATE TABLE orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        payment_status TEXT DEFAULT 'unpaid',
        order_token_hash TEXT
    )
    """)
    for statement in DELIVERY_SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.commit()
    conn.close()
    return db


def test_lease_fencing_prevents_expired_worker_from_cancelling_active_worker(test_db):
    """
    Race condition test:
    Worker A claims a job (lease T1).
    Worker A takes longer than 3 minutes -> lease expires.
    Worker B claims the job (lease T2 != T1, state='processing', attempts=2).
    Worker A then encounters an error (e.g. timeout) and calls finish(..., exc, lease_until=T1).
    Fencing check ensures Worker A's update does NOT cancel Worker B's active lease or set state='failed'.
    Worker B finishes successfully and sets state='delivered'.
    """
    with sqlite3.connect(test_db) as conn:
        job_id = enqueue(conn, order_id=1, kind="admin_email")
        conn.commit()

    # 1. Worker A claims the job
    worker_a_job = claim_due(test_db)
    assert worker_a_job is not None
    assert worker_a_job["id"] == job_id
    assert worker_a_job["attempts"] == 1
    lease_a = worker_a_job["lease_until"]

    # 2. Simulate lease expiration: lease_until set in the past
    past_time = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with sqlite3.connect(test_db) as conn:
        conn.execute("UPDATE delivery_jobs SET lease_until=? WHERE id=?", (past_time, job_id))

    # 3. Worker B claims the job while Worker A is still running
    worker_b_job = claim_due(test_db)
    assert worker_b_job is not None
    assert worker_b_job["id"] == job_id
    assert worker_b_job["attempts"] == 2
    lease_b = worker_b_job["lease_until"]
    assert lease_b != lease_a

    # 4. Worker A finally fails with a timeout and attempts to report failure with its old lease
    finish(test_db, job_id, TimeoutError("Worker A upload timed out"), lease_until=lease_a)

    # 5. Verify Worker B's active processing was NOT clobbered!
    with sqlite3.connect(test_db) as conn:
        conn.row_factory = sqlite3.Row
        current = conn.execute("SELECT * FROM delivery_jobs WHERE id=?", (job_id,)).fetchone()
        assert current["state"] == "processing", "Worker A must not clobber Worker B's active processing state!"
        assert current["lease_until"] == lease_b, "Worker A must not clobber Worker B's active lease!"

    # 6. Worker B succeeds and marks delivered
    finish(test_db, job_id, None, lease_until=lease_b)

    with sqlite3.connect(test_db) as conn:
        conn.row_factory = sqlite3.Row
        final_job = conn.execute("SELECT * FROM delivery_jobs WHERE id=?", (job_id,)).fetchone()
        assert final_job["state"] == "delivered"
        assert final_job["delivered_at"] is not None
        assert final_job["lease_until"] is None


def test_max_attempts_stops_infinite_retry_of_poison_pill(test_db):
    """
    Max attempts test:
    A permanently failing job is retried with backoff until max_attempts (10) is reached.
    Once attempts == max_attempts, claim_due stops selecting the job.
    Job remains in DB as 'failed' with attempts=10 and error recorded for monitoring.
    """
    with sqlite3.connect(test_db) as conn:
        job_id = enqueue(conn, order_id=2, kind="admin_email")
        conn.commit()

    max_limit = 10
    for attempt in range(1, max_limit + 1):
        # Force due time to now for immediate test execution
        with sqlite3.connect(test_db) as conn:
            conn.execute("UPDATE delivery_jobs SET next_attempt_at=? WHERE id=?", (utc_now(), job_id))

        job = claim_due(test_db, max_attempts=max_limit)
        assert job is not None
        assert job["id"] == job_id
        assert job["attempts"] == attempt

        # Fail the job
        finish(test_db, job_id, RuntimeError("Permanent poison error"), lease_until=job["lease_until"])

    # After max_limit attempts, make sure next_attempt_at is due
    with sqlite3.connect(test_db) as conn:
        conn.execute("UPDATE delivery_jobs SET next_attempt_at=? WHERE id=?", (utc_now(), job_id))

    # Next claim attempt must return None — job is quarantined!
    job_after_max = claim_due(test_db, max_attempts=max_limit)
    assert job_after_max is None

    # Inspect job in database
    with sqlite3.connect(test_db) as conn:
        conn.row_factory = sqlite3.Row
        quarantined = conn.execute("SELECT * FROM delivery_jobs WHERE id=?", (job_id,)).fetchone()
        assert quarantined["state"] == "failed"
        assert quarantined["attempts"] == max_limit
        assert quarantined["last_error"] == "RuntimeError"
