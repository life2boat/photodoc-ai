"""Durable SQLite delivery outbox. External I/O is performed outside transactions."""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_MAX_ATTEMPTS = int(os.getenv("DELIVERY_MAX_ATTEMPTS", "10"))


DELIVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_jobs (
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
CREATE INDEX IF NOT EXISTS idx_delivery_due ON delivery_jobs(state, next_attempt_at, lease_until);
CREATE UNIQUE INDEX IF NOT EXISTS idx_paid_email_once ON delivery_jobs(order_id) WHERE kind = 'paid_email';
CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_email_once ON delivery_jobs(order_id) WHERE kind = 'admin_email';
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def enqueue(conn: sqlite3.Connection, *, order_id: int, kind: str,
            local_path: str | None = None, original_name: str | None = None,
            remote_path: str | None = None, sha256: str | None = None,
            size: int | None = None) -> str:
    job_id = uuid.uuid4().hex
    now = utc_now()
    conn.execute(
        "INSERT OR IGNORE INTO delivery_jobs "
        "(id, order_id, kind, local_path, original_name, remote_path, content_sha256, "
        "content_size, next_attempt_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, order_id, kind, local_path, original_name, remote_path, sha256, size, now, now),
    )
    return job_id


def claim_due(db_path: Path, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> dict | None:
    now = utc_now()
    lease = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM delivery_jobs WHERE "
            "((state IN ('pending', 'failed') AND attempts < ? AND next_attempt_at <= ?) OR "
            "(state = 'processing' AND lease_until <= ?)) "
            "ORDER BY created_at, id LIMIT 1", (max_attempts, now, now),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        conn.execute(
            "UPDATE delivery_jobs SET state='processing', attempts=attempts+1, lease_until=? WHERE id=?",
            (lease, row["id"]),
        )
        conn.commit()
        claimed = dict(row)
        claimed["lease_until"] = lease
        claimed["attempts"] = row["attempts"] + 1
        claimed["state"] = "processing"
        return claimed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finish(db_path: Path, job_id: str, error: Exception | None = None, lease_until: str | None = None) -> None:
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        if error is None:
            if lease_until is not None:
                conn.execute(
                    "UPDATE delivery_jobs SET state='delivered', lease_until=NULL, delivered_at=?, last_error=NULL "
                    "WHERE id=? AND state='processing' AND lease_until=?", (utc_now(), job_id, lease_until),
                )
            else:
                conn.execute(
                    "UPDATE delivery_jobs SET state='delivered', lease_until=NULL, delivered_at=?, last_error=NULL "
                    "WHERE id=? AND state='processing'", (utc_now(), job_id),
                )
        else:
            row = conn.execute("SELECT attempts FROM delivery_jobs WHERE id=?", (job_id,)).fetchone()
            if row is not None:
                attempts = row[0]
                delay = min(3600, 5 * (2 ** min(max(0, attempts - 1), 9)))
                next_time = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
                if lease_until is not None:
                    conn.execute(
                        "UPDATE delivery_jobs SET state='failed', lease_until=NULL, next_attempt_at=?, last_error=? "
                        "WHERE id=? AND state='processing' AND lease_until=?",
                        (next_time, type(error).__name__, job_id, lease_until),
                    )
                else:
                    conn.execute(
                        "UPDATE delivery_jobs SET state='failed', lease_until=NULL, next_attempt_at=?, last_error=? "
                        "WHERE id=? AND state='processing'",
                        (next_time, type(error).__name__, job_id),
                    )
        conn.commit()
    finally:
        conn.close()


def process_due(db_path: Path, deliver, limit: int = 20) -> int:
    processed = 0
    for _ in range(limit):
        job = claim_due(db_path)
        if job is None:
            break
        lease = job.get("lease_until")
        try:
            deliver(job)
        except Exception as exc:
            logging.warning("Delivery job %s failed: %s", job["id"], type(exc).__name__)
            finish(db_path, job["id"], exc, lease_until=lease)
        else:
            finish(db_path, job["id"], lease_until=lease)
        processed += 1
    return processed


def worker_loop(db_path: Path, deliver, stop: threading.Event, interval: float = 2.0) -> None:
    while not stop.is_set():
        try:
            process_due(db_path, deliver)
        except Exception:
            logging.exception("Delivery worker iteration failed")
        stop.wait(interval)
