"""At-most-one automatic SMTP attempt after an ambiguous DATA outcome.

Sending is durable before external I/O. Only a proven pre-DATA failure is retryable;
unknown outcomes require operator reconciliation, never automatic retransmission.
"""
import sqlite3
import uuid
from datetime import datetime, timezone

from sqlite_busy import BUSY_TIMEOUT, retry_busy

EMAIL_SCHEMA = """
CREATE TABLE IF NOT EXISTS paid_email_outbox (
    order_id INTEGER PRIMARY KEY REFERENCES orders(id),
    state TEXT NOT NULL CHECK(state IN ('pending','retry','sending','sent','uncertain')),
    attempt_token TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    accepted_at TEXT,
    last_error TEXT
);
"""


def now():
    return datetime.now(timezone.utc).isoformat()


@retry_busy
def initialize(db):
    """Additive migration; preserve ambiguous legacy attempts instead of resending."""
    conn = sqlite3.connect(db, timeout=BUSY_TIMEOUT)
    try:
        # Prepared databases must remain inspectable by read-only startup probes.
        # Avoid even INSERT OR IGNORE when no migration/backfill is necessary.
        table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paid_email_outbox'").fetchone()
        jobs = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_jobs'").fetchone()
        if table and (not jobs or not conn.execute("""
                SELECT 1 FROM orders o JOIN delivery_jobs j ON j.order_id=o.id
                WHERE j.kind='paid_email' AND NOT EXISTS
                    (SELECT 1 FROM paid_email_outbox e WHERE e.order_id=o.id) LIMIT 1
                """).fetchone()):
            return
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(EMAIL_SCHEMA)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='delivery_jobs'").fetchone():
            conn.execute("""
                INSERT OR IGNORE INTO paid_email_outbox(order_id,state,updated_at,accepted_at,last_error)
                SELECT o.id, CASE WHEN o.payment_email_sent_at IS NOT NULL THEN 'sent'
                    WHEN j.attempts>0 OR j.state IN ('processing','delivered') THEN 'uncertain'
                    ELSE 'pending' END, ?, o.payment_email_sent_at,
                    CASE WHEN o.payment_email_sent_at IS NULL AND
                        (j.attempts>0 OR j.state IN ('processing','delivered'))
                        THEN 'LegacyOutcomeUnknown' ELSE NULL END
                FROM orders o JOIN delivery_jobs j ON j.order_id=o.id AND j.kind='paid_email'
            """, (now(),))
        conn.commit()
    finally:
        conn.close()


@retry_busy
def claim(db, order_id, max_attempts=10):
    conn = sqlite3.connect(db, timeout=BUSY_TIMEOUT)
    try:
        conn.execute("BEGIN IMMEDIATE")
        order = conn.execute("SELECT payment_status,payment_email_sent_at FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order or order[0] != "paid":
            return "blocked", None
        conn.execute("INSERT OR IGNORE INTO paid_email_outbox(order_id,state,updated_at) VALUES(?,'pending',?)", (order_id, now()))
        row = conn.execute("SELECT state,accepted_at,attempts FROM paid_email_outbox WHERE order_id=?", (order_id,)).fetchone()
        if order[1] or row[0] == "sent":
            stamp = order[1] or row[1] or now()
            conn.execute("UPDATE orders SET payment_email_sent_at=? WHERE id=?", (stamp, order_id))
            conn.execute("UPDATE paid_email_outbox SET state='sent',accepted_at=?,updated_at=? WHERE order_id=?", (stamp, now(), order_id))
            conn.commit()
            return "sent", None
        if row[0] not in ("pending", "retry") or row[2] >= max_attempts:
            return "blocked", None
        token = uuid.uuid4().hex
        conn.execute("UPDATE paid_email_outbox SET state='sending',attempt_token=?,attempts=attempts+1,updated_at=?,last_error=NULL WHERE order_id=?", (token, now(), order_id))
        conn.commit()
        return "send", token
    finally:
        conn.close()


@retry_busy
def record(db, order_id, token, state, error=None):
    """Retry only this SQL transaction; a failed acknowledgment never replays SMTP."""
    conn = sqlite3.connect(db, timeout=BUSY_TIMEOUT)
    try:
        conn.execute("BEGIN IMMEDIATE")
        stamp = now()
        result = conn.execute("""UPDATE paid_email_outbox SET state=?,updated_at=?,accepted_at=?,last_error=?
            WHERE order_id=? AND attempt_token=? AND state IN ('sending','uncertain')""",
            (state, stamp, stamp if state == "sent" else None, error, order_id, token))
        if result.rowcount != 1:
            raise RuntimeError("Paid email attempt ownership lost")
        if state == "sent":
            conn.execute("UPDATE orders SET payment_email_sent_at=? WHERE id=?", (stamp, order_id))
        conn.commit()
    finally:
        conn.close()
