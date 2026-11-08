"""Bounded retention for paid but undelivered orders.

Prototype. The rule it encodes: a paid order is an obligation, so its files are never
deleted by a timer. Instead the order is **escalated** once it stays undelivered longer than
the hold window (7 days by default), so a human resolves it — deliver the files or refund the
payment — and only then does the normal cleanup path free the storage.

This module never deletes files and never changes payment state; it only reads and records.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

DEFAULT_HOLD_HOURS = 168  # 7 days

SCHEMA = """
CREATE TABLE IF NOT EXISTS paid_hold_audit (
    order_id INTEGER PRIMARY KEY,
    job_id TEXT,
    first_seen_at TEXT NOT NULL,
    age_hours REAL NOT NULL,
    escalated_at TEXT,
    state TEXT NOT NULL CHECK(state IN ('watching', 'escalated')),
    note TEXT
);
"""


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def list_holds(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    hold_hours: int = DEFAULT_HOLD_HOURS,
) -> list[dict[str, Any]]:
    """Paid orders that still have an undelivered file job."""
    current = now or datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT j.id AS job_id, j.order_id, j.created_at, j.local_path, j.state "
        "FROM delivery_jobs j JOIN orders o ON o.id = j.order_id "
        "WHERE o.payment_status = 'paid' AND j.kind IN ('file','info') "
        "  AND j.state != 'delivered' AND j.local_path IS NOT NULL"
    ).fetchall()
    holds = []
    for row in rows:
        try:
            created = datetime.fromisoformat(str(row[2]))
        except ValueError:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_hours = (current - created).total_seconds() / 3600
        holds.append({
            "order_id": row[1],
            "job_id": row[0],
            "local_path": row[3],
            "state": row[4],
            "age_hours": round(age_hours, 2),
            "escalated": age_hours >= hold_hours,
        })
    return holds


def flag_escalations(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    hold_hours: int = DEFAULT_HOLD_HOURS,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Record holds and escalate those past the window. Never deletes anything."""
    current = now or datetime.now(timezone.utc)
    now_iso = _iso(current)
    conn.executescript(SCHEMA)
    holds = list_holds(conn, now=current, hold_hours=hold_hours)

    summary = {"hold_hours": hold_hours, "dry_run": dry_run, "watching": 0,
               "escalated": 0, "newly_escalated": 0}

    for hold in holds:
        state = "escalated" if hold["escalated"] else "watching"
        summary[state] += 1
        if dry_run:
            continue
        existing = conn.execute(
            "SELECT state FROM paid_hold_audit WHERE order_id=?", (hold["order_id"],)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO paid_hold_audit (order_id, job_id, first_seen_at, age_hours, "
                "escalated_at, state, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (hold["order_id"], hold["job_id"], now_iso, hold["age_hours"],
                 now_iso if hold["escalated"] else None, state,
                 "paid but undelivered" if hold["escalated"] else "within the hold window"),
            )
            if hold["escalated"]:
                summary["newly_escalated"] += 1
        elif existing[0] != "escalated" and hold["escalated"]:
            conn.execute(
                "UPDATE paid_hold_audit SET state='escalated', escalated_at=?, age_hours=?, "
                "note='paid but undelivered past the hold window' WHERE order_id=?",
                (now_iso, hold["age_hours"], hold["order_id"]),
            )
            summary["newly_escalated"] += 1
        else:
            conn.execute("UPDATE paid_hold_audit SET age_hours=? WHERE order_id=?",
                         (hold["age_hours"], hold["order_id"]))
    conn.commit()
    return summary


def operator_queue(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Orders a human must resolve: deliver the files or refund the payment."""
    conn.executescript(SCHEMA)
    rows = conn.execute(
        "SELECT order_id, job_id, first_seen_at, age_hours, escalated_at, note "
        "FROM paid_hold_audit WHERE state='escalated' ORDER BY age_hours DESC"
    ).fetchall()
    keys = ("order_id", "job_id", "first_seen_at", "age_hours", "escalated_at", "note")
    return [dict(zip(keys, row)) for row in rows]
