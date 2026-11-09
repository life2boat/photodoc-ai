"""Retention and data-lifecycle background worker.

Connects retention purge (72h unpaid source purge), quarantine release (24h grace),
paid order escalation hold protection (7 days undelivered), and backup rotation
into the application lifecycle with multi-instance lease fencing and telemetry.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import backup_rotation
import paid_hold
import retention

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS retention_scheduler_lease (
    lease_name TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    lease_until TEXT NOT NULL,
    acquired_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS retention_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT NOT NULL,
    cycle_started_at TEXT NOT NULL,
    cycle_completed_at TEXT NOT NULL,
    purged_expired_count INTEGER NOT NULL DEFAULT 0,
    bytes_freed INTEGER NOT NULL DEFAULT 0,
    quarantines_released INTEGER NOT NULL DEFAULT 0,
    holds_watching INTEGER NOT NULL DEFAULT 0,
    holds_escalated INTEGER NOT NULL DEFAULT 0,
    backups_removed INTEGER NOT NULL DEFAULT 0,
    errors_count INTEGER NOT NULL DEFAULT 0,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_retention_telemetry_time ON retention_telemetry(cycle_completed_at);
"""


def initialize(conn: sqlite3.Connection) -> None:
    """Initialize lease and telemetry tables."""
    conn.executescript(SCHEMA)
    conn.commit()


def validate_retention_safety(
    db_path: Path,
    uploads_dir: Path,
    *,
    ttl_hours: int | None = None,
) -> None:
    """Validate retention safety constraints before running background worker."""
    hours = int(os.getenv("RETENTION_HOURS", "72") if ttl_hours is None else ttl_hours)
    if hours < 24:
        raise RuntimeError(
            f"Retention safety violation: RETENTION_HOURS must be >= 24 (configured: {hours})"
        )

    resolved_uploads = Path(uploads_dir).resolve()
    if not resolved_uploads.is_dir():
        raise RuntimeError(
            f"Retention safety violation: uploads directory does not exist ({resolved_uploads})"
        )

    # Prevent running against filesystem root or parent directories
    if resolved_uploads == resolved_uploads.parent:
        raise RuntimeError("Retention safety violation: uploads directory cannot be filesystem root")

    # If in production context, verify destructive operations are not accidentally triggered
    if os.getenv("PHOTODOC_ENVIRONMENT") == "production" and os.getenv("DEPLOYMENT_BLOCKED", "true").lower() == "true":
        logger.info("Retention worker running in read-only / protected staging mode")


def acquire_retention_lease(
    conn: sqlite3.Connection,
    instance_id: str,
    *,
    lease_duration_seconds: int = 60,
    lease_name: str = "retention_worker",
    now: datetime | None = None,
) -> bool:
    """Acquire or renew scheduler lease for multi-instance fencing."""
    current = now or datetime.now(timezone.utc)
    now_iso = current.isoformat()
    lease_until = (current + timedelta(seconds=lease_duration_seconds)).isoformat()

    initialize(conn)

    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        row = cursor.execute(
            "SELECT instance_id, lease_until FROM retention_scheduler_lease WHERE lease_name=?",
            (lease_name,),
        ).fetchone()

        if row is None:
            cursor.execute(
                "INSERT INTO retention_scheduler_lease (lease_name, instance_id, lease_until, acquired_at) "
                "VALUES (?, ?, ?, ?)",
                (lease_name, instance_id, lease_until, now_iso),
            )
            conn.commit()
            return True

        current_owner, current_lease_until = row
        # Can acquire if lease expired or if this instance already owns it
        if current_owner == instance_id or current_lease_until < now_iso:
            cursor.execute(
                "UPDATE retention_scheduler_lease SET instance_id=?, lease_until=?, acquired_at=? "
                "WHERE lease_name=? AND (lease_until < ? OR instance_id=?)",
                (instance_id, lease_until, now_iso, lease_name, now_iso, instance_id),
            )
            acquired = cursor.rowcount == 1
            conn.commit()
            return acquired

        conn.commit()
        return False
    except Exception:
        conn.rollback()
        return False


def release_retention_lease(
    conn: sqlite3.Connection,
    instance_id: str,
    *,
    lease_name: str = "retention_worker",
) -> None:
    """Voluntarily release scheduler lease on shutdown."""
    try:
        conn.execute(
            "UPDATE retention_scheduler_lease SET lease_until='1970-01-01T00:00:00+00:00' "
            "WHERE lease_name=? AND instance_id=?",
            (lease_name, instance_id),
        )
        conn.commit()
    except Exception as exc:
        logger.warning("Failed to release retention lease %s: %s", lease_name, exc)


def run_retention_cycle(
    db_path: Path,
    uploads_dir: Path,
    *,
    instance_id: str | None = None,
    backup_dir: Path | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    lease_duration_seconds: int = 120,
) -> dict[str, Any]:
    """Execute a coordinated retention, purge, quarantine, and hold-escalation cycle."""
    inst_id = instance_id or uuid.uuid4().hex
    current = now or datetime.now(timezone.utc)
    cycle_start = current.isoformat()

    validate_retention_safety(db_path, uploads_dir)

    conn = sqlite3.connect(db_path, timeout=10)
    try:
        # Step 0: Acquire lease fence
        acquired = acquire_retention_lease(
            conn,
            inst_id,
            lease_duration_seconds=lease_duration_seconds,
            now=current,
        )
        if not acquired and not dry_run:
            return {
                "ok": False,
                "reason": "lease_held_by_peer",
                "instance_id": inst_id,
                "cycle_started_at": cycle_start,
            }

        # Step 1: Purge expired unpaid files (72h retention)
        expired_summary = retention.purge_expired(
            db_path,
            uploads_dir=uploads_dir,
            now=current,
            dry_run=dry_run,
            orphan_scan=True,
        )

        # Step 2: Purge quarantines (24h grace release with payment restore check)
        quarantine_summary = retention.purge_quarantines(
            db_path,
            uploads_dir=uploads_dir,
            now=current,
            dry_run=dry_run,
        )

        # Step 3: Flag undelivered paid holds (> 7 days escalated; never delete)
        hold_summary = paid_hold.flag_escalations(
            conn,
            now=current,
            dry_run=dry_run,
        )

        # Step 4: Backup rotation if enabled
        rotation_summary: dict[str, Any] = {"removed": [], "kept": 0}
        backup_env_enabled = os.getenv("BACKUP_ROTATION_ENABLED", "0") == "1"
        b_dir = backup_dir or (Path(os.getenv("BACKUP_DIR")) if os.getenv("BACKUP_DIR") else None)
        if backup_env_enabled and b_dir and b_dir.is_dir():
            rotation_summary = backup_rotation.rotate(
                b_dir,
                now=current,
                dry_run=dry_run,
            )

        cycle_end = (datetime.now(timezone.utc) if now is None else now).isoformat()

        errors_count = (
            len(expired_summary.get("errors", []))
            + len(quarantine_summary.get("errors", []))
            + len(rotation_summary.get("errors", []))
        )

        summary = {
            "ok": True,
            "instance_id": inst_id,
            "cycle_started_at": cycle_start,
            "cycle_completed_at": cycle_end,
            "expired_purge": expired_summary,
            "quarantine_purge": quarantine_summary,
            "paid_hold": hold_summary,
            "backup_rotation": rotation_summary,
            "errors_count": errors_count,
        }

        # Record telemetry without PII
        if not dry_run:
            try:
                conn.execute(
                    "INSERT INTO retention_telemetry "
                    "(instance_id, cycle_started_at, cycle_completed_at, purged_expired_count, "
                    "bytes_freed, quarantines_released, holds_watching, holds_escalated, "
                    "backups_removed, errors_count, details_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        inst_id,
                        cycle_start,
                        cycle_end,
                        expired_summary.get("deleted", 0),
                        expired_summary.get("bytes_freed", 0),
                        quarantine_summary.get("released", 0),
                        hold_summary.get("watching", 0),
                        hold_summary.get("escalated", 0),
                        len(rotation_summary.get("removed", [])),
                        errors_count,
                        json.dumps({
                            "retention_hours": expired_summary.get("retention_hours"),
                            "held_paid": expired_summary.get("held_paid"),
                            "held_pending": expired_summary.get("held_pending"),
                        }),
                    ),
                )
                conn.commit()
            except Exception as tel_err:
                logger.warning("Failed to record retention telemetry: %s", tel_err)

        return summary
    finally:
        conn.close()


def retention_worker_loop(
    db_path: Path,
    stop_event: threading.Event,
    *,
    uploads_dir: Path | None = None,
    interval_seconds: float = 3600.0,
    instance_id: str | None = None,
) -> None:
    """Continuous background loop for application lifecycle integration."""
    inst_id = instance_id or f"worker-{uuid.uuid4().hex[:8]}"
    uploads = uploads_dir or (db_path.parent / "uploads")
    logger.info("Starting retention worker loop [instance=%s, interval=%ss]", inst_id, interval_seconds)

    while not stop_event.is_set():
        try:
            summary = run_retention_cycle(
                db_path,
                uploads,
                instance_id=inst_id,
            )
            if summary.get("ok"):
                logger.info(
                    "Retention cycle finished: purged=%s, quarantines_released=%s, escalated_holds=%s",
                    summary.get("expired_purge", {}).get("deleted", 0),
                    summary.get("quarantine_purge", {}).get("released", 0),
                    summary.get("paid_hold", {}).get("escalated", 0),
                )
            else:
                logger.info("Retention cycle skipped: %s", summary.get("reason"))
        except Exception as exc:
            logger.error("Error in retention worker loop: %s", exc, exc_info=True)

        # Sleep with early wake-up support via Event
        stop_event.wait(interval_seconds)

    # Clean release on shutdown
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            release_retention_lease(conn, inst_id)
    except Exception:
        pass
    logger.info("Retention worker loop stopped cleanly [instance=%s]", inst_id)
