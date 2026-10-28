"""Durable SQLite delivery outbox with automated photo cleanup and privacy guards. External I/O is performed outside transactions."""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_MAX_ATTEMPTS = int(os.getenv("DELIVERY_MAX_ATTEMPTS", "10"))
DEFAULT_UPLOADS_DIR = Path(os.getenv("UPLOAD_DIR", "/app/uploads"))
_UPLOADS_DIR = DEFAULT_UPLOADS_DIR


def set_uploads_dir(path: Path) -> None:
    global _UPLOADS_DIR
    _UPLOADS_DIR = Path(path)


def get_uploads_dir() -> Path:
    return _UPLOADS_DIR


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
    cleanup_state TEXT NOT NULL DEFAULT 'pending' CHECK(cleanup_state IN ('pending', 'processing', 'completed', 'failed', 'not_applicable')),
    cleanup_phase TEXT NOT NULL DEFAULT 'none' CHECK(cleanup_phase IN ('none', 'pending', 'deleting', 'deleted', 'failed')),
    local_deleted_at TEXT,
    cleanup_error TEXT,
    cleanup_attempts INTEGER NOT NULL DEFAULT 0,
    cleanup_lease_until TEXT,
    UNIQUE(order_id, kind, remote_path)
);
CREATE INDEX IF NOT EXISTS idx_delivery_due ON delivery_jobs(state, next_attempt_at, lease_until);
CREATE INDEX IF NOT EXISTS idx_cleanup_due ON delivery_jobs(state, cleanup_state, cleanup_lease_until);
CREATE UNIQUE INDEX IF NOT EXISTS idx_paid_email_once ON delivery_jobs(order_id) WHERE kind = 'paid_email';
CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_email_once ON delivery_jobs(order_id) WHERE kind = 'admin_email';
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def enqueue(conn: sqlite3.Connection, *, order_id: int, kind: str,
            local_path: str | None = None, original_name: str | None = None,
            remote_path: str | None = None, sha256: str | None = None,
            size: int | None = None) -> str:
    job_id = uuid.uuid4().hex
    now = utc_now()
    if kind in ("admin_email", "paid_email"):
        cleanup_state = "not_applicable"
        cleanup_phase = "none"
    else:
        cleanup_state = "pending"
        cleanup_phase = "pending"
    conn.execute(
        "INSERT OR IGNORE INTO delivery_jobs "
        "(id, order_id, kind, local_path, original_name, remote_path, content_sha256, "
        "content_size, next_attempt_at, created_at, cleanup_state, cleanup_phase) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, order_id, kind, local_path, original_name, remote_path, sha256, size, now, now, cleanup_state, cleanup_phase),
    )
    return job_id


def quarantine_expired(conn: sqlite3.Connection, now: str, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> int:
    cursor = conn.execute(
        "UPDATE delivery_jobs SET state = 'failed', lease_until = NULL, "
        "last_error = COALESCE(last_error, 'LeaseExpired') "
        "WHERE state = 'processing' AND lease_until <= ? AND attempts >= ?",
        (now, max_attempts),
    )
    return cursor.rowcount


def claim_due(db_path: Path, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> dict | None:
    now = utc_now()
    lease = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        quarantine_expired(conn, now, max_attempts)
        row = conn.execute(
            "SELECT * FROM delivery_jobs WHERE "
            "((state IN ('pending', 'failed') AND attempts < ? AND next_attempt_at <= ?) OR "
            "(state = 'processing' AND attempts < ? AND lease_until <= ?)) "
            "ORDER BY created_at, id LIMIT 1", (max_attempts, now, max_attempts, now),
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


def finish(db_path: Path, job_id: str, error: Exception | None = None, lease_until: str | None = None) -> bool:
    """
    Finalizes delivery attempt in SQLite.
    Returns True if delivery succeeded and was committed under the active lease.
    Returns False if delivery failed or if the lease expired / was stolen by another worker.
    """
    conn = sqlite3.connect(db_path, timeout=10)
    committed = False
    try:
        if error is None:
            if lease_until is not None:
                cursor = conn.execute(
                    "UPDATE delivery_jobs SET state='delivered', lease_until=NULL, delivered_at=?, last_error=NULL "
                    "WHERE id=? AND state='processing' AND lease_until=?", (utc_now(), job_id, lease_until),
                )
            else:
                cursor = conn.execute(
                    "UPDATE delivery_jobs SET state='delivered', lease_until=NULL, delivered_at=?, last_error=NULL "
                    "WHERE id=? AND state='processing'", (utc_now(), job_id),
                )
            committed = cursor.rowcount > 0
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
            committed = False
        conn.commit()
        return committed
    finally:
        conn.close()


def _is_cleanup_lease_valid(conn: sqlite3.Connection, job_id: str, token: str) -> bool:
    """
    Verifies that this worker still holds the active lease token before any filesystem mutation.
    Returns False if the lease expired or was acquired by another worker.
    """
    row = conn.execute(
        "SELECT cleanup_lease_until FROM delivery_jobs "
        "WHERE id=? AND cleanup_state='processing'",
        (job_id,),
    ).fetchone()
    if not row or row[0] != token:
        return False
    # Verify expiration timestamp part of token has not passed
    lease_ts = token.split("#")[0]
    return lease_ts > utc_now()


def _mark_cleanup_completed(db_path: Path, job_id: str, token: str | None = None) -> bool:
    """
    Atomically records cleanup completion if the worker still holds the lease.
    """
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        now_ts = utc_now()
        if token is not None:
            cursor = conn.execute(
                "UPDATE delivery_jobs SET cleanup_state='completed', cleanup_phase='deleted', local_deleted_at=?, "
                "cleanup_lease_until=NULL, cleanup_error=NULL "
                "WHERE id=? AND cleanup_state='processing' AND cleanup_lease_until=?",
                (now_ts, job_id, token),
            )
        else:
            cursor = conn.execute(
                "UPDATE delivery_jobs SET cleanup_state='completed', cleanup_phase='deleted', local_deleted_at=?, "
                "cleanup_lease_until=NULL, cleanup_error=NULL WHERE id=?",
                (now_ts, job_id),
            )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def _mark_cleanup_failed(db_path: Path, job_id: str, token: str | None = None, error: str = "") -> bool:
    """
    Atomically records cleanup failure with bounded exponential backoff if the worker still holds the lease.
    """
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        row = conn.execute("SELECT cleanup_attempts FROM delivery_jobs WHERE id=?", (job_id,)).fetchone()
        attempts = row[0] if row else 1
        delay = min(3600, 5 * (2 ** min(max(0, attempts - 2), 8))) if attempts > 1 else 0
        backoff_until = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        if token is not None:
            cursor = conn.execute(
                "UPDATE delivery_jobs SET cleanup_state='failed', cleanup_phase='failed', cleanup_lease_until=?, "
                "cleanup_error=? WHERE id=? AND cleanup_state='processing' AND cleanup_lease_until=?",
                (backoff_until, error, job_id, token),
            )
        else:
            cursor = conn.execute(
                "UPDATE delivery_jobs SET cleanup_state='failed', cleanup_phase='failed', cleanup_lease_until=?, "
                "cleanup_error=? WHERE id=?",
                (backoff_until, error, job_id),
            )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def perform_job_cleanup(
    db_path: Path,
    job_id: str,
    uploads_dir: Path | None = None,
    pre_unlink_hook: Any = None,
    clock: Any = None,
    lease_duration_seconds: int = 120,
) -> bool:
    """
    Safely and idempotently removes a delivered photo or info.txt file from local disk.

    Strict Safety Guards:
    1. Preconditions: state == 'delivered' and kind in ('file', 'info').
    2. Lease & Concurrency: atomically acquires a unique fencing lease token to prevent worker collisions.
    3. Confinement: path MUST be strictly inside uploads_dir without '..' or symlinks.
    4. Isolation: local file MUST match the order_id prefix to prevent cross-order file manipulation.
    5. Identity check: checks size and sha256 against delivery_jobs metadata before unlinking.
    6. Dependency check: checks that NO other unfinished delivery job references this local_path.
    7. Durable Intent: commits cleanup_phase='deleting' before unlinking to enable crash recovery.
    8. Pre-unlink fencing: verifies active lease ownership in write transaction before unlinking.
    9. Idempotency & Crash recovery: if file is already deleted and cleanup_phase=='deleting',
       records completed cleanly only if conditional finalization succeeds.
       If deleted without intent, flags as MissingSourceBeforeCleanup anomaly.
    10. Failure resilience: cleanup error never marks delivery state back to pending; applies bounded backoff.
    """
    if uploads_dir is None:
        uploads_dir = get_uploads_dir()
    uploads_resolved = uploads_dir.resolve()

    now = clock() if clock is not None else utc_now()
    now_dt = datetime.fromisoformat(now) if clock is not None else datetime.now(timezone.utc)
    lease_ts = (now_dt + timedelta(seconds=lease_duration_seconds)).isoformat()
    nonce = uuid.uuid4().hex[:8]

    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    token = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            "SELECT id, order_id, kind, local_path, content_sha256, content_size, state, "
            "cleanup_state, cleanup_phase, cleanup_attempts, cleanup_lease_until, cleanup_error "
            "FROM delivery_jobs WHERE id=?",
            (job_id,),
        ).fetchone()

        if not job:
            conn.commit()
            return False

        # Only delivered jobs can have their local files cleaned up
        if job["state"] != "delivered":
            conn.commit()
            return False

        # Email jobs don't have local uploads to clean
        if job["kind"] not in ("file", "info"):
            conn.execute(
                "UPDATE delivery_jobs SET cleanup_state='not_applicable', cleanup_phase='none' WHERE id=?", (job_id,)
            )
            conn.commit()
            return False

        # Already completed cleanup
        if job["cleanup_state"] == "completed":
            conn.commit()
            return True

        # Non-retryable anomalies: never auto-retry unexplained missing sources
        if job["cleanup_error"] in ("HistoricalMissingSource", "MissingSourceBeforeCleanup", "MissingLocalPath"):
            conn.commit()
            return False

        # Check if another active worker holds a valid cleanup lease
        if job["cleanup_state"] == "processing" and job["cleanup_lease_until"]:
            active_ts = job["cleanup_lease_until"].split("#")[0]
            if active_ts > now:
                conn.commit()
                return False

        # Check if failed job is still in backoff or exceeded max attempts
        if job["cleanup_state"] == "failed":
            if job["cleanup_attempts"] >= 10:
                conn.commit()
                return False
            if job["cleanup_lease_until"]:
                backoff_ts = job["cleanup_lease_until"].split("#")[0]
                if backoff_ts > now:
                    conn.commit()
                    return False

        next_attempt = job["cleanup_attempts"] + 1
        token = f"{lease_ts}#{next_attempt}#{nonce}"

        # Atomically claim cleanup processing with fencing token
        cursor = conn.execute(
            "UPDATE delivery_jobs SET cleanup_state='processing', cleanup_lease_until=?, "
            "cleanup_attempts=? WHERE id=?",
            (token, next_attempt, job_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return False
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    raw_local_path = job["local_path"]
    if not raw_local_path:
        _mark_cleanup_failed(db_path, job_id, token, "MissingLocalPath")
        return False

    path_obj = Path(raw_local_path)

    # Path traversal check
    if ".." in raw_local_path or any(part == ".." for part in path_obj.parts):
        _mark_cleanup_failed(db_path, job_id, token, "PathTraversalDetected")
        return False

    # Symlink protection on input path
    if path_obj.is_symlink():
        _mark_cleanup_failed(db_path, job_id, token, "SymlinkDetected")
        return False

    try:
        target_resolved = path_obj.resolve()
    except Exception as exc:
        _mark_cleanup_failed(db_path, job_id, token, f"PathResolveError: {exc}")
        return False

    # Uploads confinement check
    if uploads_resolved not in target_resolved.parents and target_resolved.parent != uploads_resolved:
        _mark_cleanup_failed(db_path, job_id, token, "PathOutsideUploads")
        return False

    # Symlink protection on resolved target
    if target_resolved.is_symlink():
        _mark_cleanup_failed(db_path, job_id, token, "SymlinkDetected")
        return False

    # Isolation check: file must belong to this order
    if not path_obj.name.startswith(f"{job['order_id']}_"):
        _mark_cleanup_failed(db_path, job_id, token, "OrderIdMismatchInFilename")
        return False

    # Check if file is already absent:
    # Ambiguity note:
    # When cleanup_phase == 'deleting' and path_obj does not exist, the application
    # has durable proof that deletion intent was committed by a valid lease-holding worker.
    # We conclude crash recovery took place around the unlink syscall.
    # However, if an external process or storage fault removed the file between intent
    # commitment and recovery, this cannot be distinguished at the SQLite layer from
    # the application's own unlinking. The contract guarantees durable worker intent,
    # not cryptographic proof of OS unlink provenance.
    if not path_obj.exists():
        if job["cleanup_phase"] == "deleting":
            # Durable crash recovery: app started deletion and crashed around unlink/completion
            return _mark_cleanup_completed(db_path, job_id, token)
        else:
            # Check if a sibling job with the same local_path already legally deleted the file with durable evidence
            conn = sqlite3.connect(db_path, timeout=10)
            sibling_deleted = 0
            try:
                sibling_deleted = conn.execute(
                    "SELECT COUNT(*) FROM delivery_jobs "
                    "WHERE local_path=? AND id!=? AND cleanup_state='completed' "
                    "AND cleanup_phase='deleted' AND local_deleted_at IS NOT NULL",
                    (raw_local_path, job_id),
                ).fetchone()[0]
            finally:
                conn.close()

            if sibling_deleted > 0:
                # Sibling job already performed the physical deletion with durable evidence!
                return _mark_cleanup_completed(db_path, job_id, token)

            # File missing before application deletion intent was ever established
            _mark_cleanup_failed(db_path, job_id, token, "MissingSourceBeforeCleanup")
            return False

    # Must be a regular file
    if not path_obj.is_file():
        _mark_cleanup_failed(db_path, job_id, token, "NotARegularFile")
        return False

    # Pre-delete identity verification: size
    expected_size = job["content_size"]
    try:
        actual_size = path_obj.stat().st_size
    except Exception as exc:
        _mark_cleanup_failed(db_path, job_id, token, f"StatError: {exc}")
        return False

    if expected_size is not None and actual_size != expected_size:
        _mark_cleanup_failed(
            db_path, job_id, token, f"SizeMismatchBeforeDelete: actual={actual_size} expected={expected_size}"
        )
        return False

    # Pre-delete identity verification: SHA-256
    expected_sha256 = job["content_sha256"]
    if expected_sha256:
        try:
            actual_sha256 = sha256_file(path_obj).lower()
        except Exception as exc:
            _mark_cleanup_failed(db_path, job_id, token, f"ChecksumError: {exc}")
            return False

        if actual_sha256 != expected_sha256.lower():
            _mark_cleanup_failed(
                db_path, job_id, token, f"Sha256MismatchBeforeDelete: actual={actual_sha256} expected={expected_sha256}"
            )
            return False

    # Dependency check: check if any OTHER unfinished job still references this file
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        unfinished_refs = conn.execute(
            "SELECT COUNT(*) FROM delivery_jobs "
            "WHERE local_path=? AND id!=? AND state!='delivered'",
            (raw_local_path, job_id),
        ).fetchone()[0]
    finally:
        conn.close()

    if unfinished_refs > 0:
        # File is still needed by another active task; postpone cleanup
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            conn.execute(
                "UPDATE delivery_jobs SET cleanup_state='pending', cleanup_phase='pending', cleanup_lease_until=NULL, "
                "cleanup_error='FileStillReferencedByUnfinishedJob' WHERE id=? AND cleanup_lease_until=?",
                (job_id, token),
            )
            conn.commit()
        finally:
            conn.close()
        return False

    # DURABLE INTENT PHASE:
    # Commit cleanup_phase='deleting' to the database before filesystem mutation.
    # Verifies lease ownership is still valid before recording intent.
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE delivery_jobs SET cleanup_phase='deleting' "
            "WHERE id=? AND cleanup_state='processing' AND cleanup_lease_until=?",
            (job_id, token),
        )
        if cursor.rowcount == 0:
            conn.rollback()
            return False
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # Pre-unlink hook for deterministic testing
    if pre_unlink_hook is not None:
        pre_unlink_hook(job_id, token)

    # SYNCHRONIZED DELETION & FENCING CRITICAL SECTION:
    # A short SQLite write transaction that serializes lease verification,
    # filesystem mutation, and state finalization.
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT cleanup_lease_until, cleanup_state, cleanup_attempts FROM delivery_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            return False

        current_token, current_state, current_attempts = row[0], row[1], row[2]
        if current_token != token or current_state != "processing":
            # Lease was stolen or altered by another worker; stale worker aborts without unlinking!
            conn.rollback()
            return False

        lease_ts_check = token.split("#")[0]
        now_check = clock() if clock is not None else utc_now()
        if lease_ts_check <= now_check:
            # Lease expired; abort without unlinking
            conn.rollback()
            return False

        # Worker still owns valid lease. Unlink local file:
        try:
            path_obj.unlink()
        except FileNotFoundError:
            pass  # Already unlinked
        except Exception as exc:
            delay = min(3600, 5 * (2 ** min(max(0, current_attempts - 2), 8))) if current_attempts > 1 else 0
            backoff_until = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            conn.execute(
                "UPDATE delivery_jobs SET cleanup_state='failed', cleanup_phase='failed', cleanup_lease_until=?, "
                "cleanup_error=? WHERE id=? AND cleanup_lease_until=?",
                (backoff_until, f"UnlinkError: {type(exc).__name__}: {exc}", job_id, token),
            )
            conn.commit()
            return False

        now_completed = clock() if clock is not None else utc_now()
        cursor = conn.execute(
            "UPDATE delivery_jobs SET cleanup_state='completed', cleanup_phase='deleted', local_deleted_at=?, "
            "cleanup_lease_until=NULL, cleanup_error=NULL WHERE id=? AND cleanup_lease_until=?",
            (now_completed, job_id, token),
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def process_due_cleanups(
    db_path: Path,
    uploads_dir: Path | None = None,
    limit: int = 20,
    clock: Any = None,
) -> int:
    """
    Sweeps delivery_jobs for delivered tasks that still need local file cleanup.
    Recovers from crashes and retries failed cleanups with bounded backoff.
    """
    now = clock() if clock is not None else utc_now()
    conn = sqlite3.connect(db_path, timeout=10)
    candidate_ids = []
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_jobs'"
        ).fetchone()
        if not has_table:
            return 0
        cols = {row[1] for row in conn.execute("PRAGMA table_info(delivery_jobs)").fetchall()}
        if "cleanup_state" not in cols:
            return 0
        rows = conn.execute(
            "SELECT id FROM delivery_jobs "
            "WHERE state='delivered' AND kind IN ('file', 'info') "
            "AND ("
            "  cleanup_state = 'pending' "
            "  OR (cleanup_state = 'failed' AND cleanup_attempts < 10 "
            "      AND (cleanup_lease_until IS NULL OR cleanup_lease_until <= ?) "
            "      AND (cleanup_error IS NULL OR cleanup_error NOT IN ('MissingSourceBeforeCleanup', 'HistoricalMissingSource', 'MissingLocalPath'))"
            "  ) "
            "  OR (cleanup_state = 'processing' AND cleanup_lease_until <= ?)"
            ") "
            "ORDER BY created_at, id LIMIT ?",
            (now, now, limit),
        ).fetchall()
        candidate_ids = [r[0] for r in rows]
    finally:
        conn.close()

    cleaned = 0
    for jid in candidate_ids:
        if perform_job_cleanup(db_path, jid, uploads_dir=uploads_dir, clock=clock):
            cleaned += 1
    return cleaned


def cleanup_orphaned_temporary_files(
    uploads_dir: Path,
    db_path: Path,
    max_age_seconds: int = 3600,
) -> int:
    """
    Safely cleans up orphaned temporary upload files (.incoming-*, .tmp*, etc.)
    older than max_age_seconds that are not referenced by any active delivery job.
    Never deletes order files by age alone.
    """
    if not uploads_dir.is_dir():
        return 0

    now = time.time()
    removed = 0

    referenced_paths = set()
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_jobs'"
        ).fetchone()
        if has_table:
            rows = conn.execute(
                "SELECT local_path FROM delivery_jobs WHERE local_path IS NOT NULL"
            ).fetchall()
            for r in rows:
                if r[0]:
                    try:
                        referenced_paths.add(Path(r[0]).resolve())
                    except Exception:
                        pass
    finally:
        conn.close()

    uploads_resolved = uploads_dir.resolve()
    for entry in uploads_dir.iterdir():
        # Match only recognized temporary prefixes/suffixes
        is_temp = (
            entry.name.startswith(".incoming-")
            or entry.name.startswith(".tmp")
            or entry.name.endswith(".tmp")
        )
        if not is_temp:
            # Strictly do not delete order files or unrecognized files by age!
            continue

        try:
            if entry.is_symlink():
                continue
            resolved = entry.resolve()
            if uploads_resolved not in resolved.parents and resolved.parent != uploads_resolved:
                continue
            if not entry.is_file():
                continue

            stat = entry.stat()
            age = now - stat.st_mtime
            if age < max_age_seconds:
                # Within protective threshold (in-flight upload)
                continue

            if resolved in referenced_paths:
                continue

            entry.unlink()
            removed += 1
        except Exception as exc:
            logging.warning("Failed to remove orphaned temp file %s: %s", entry.name, exc)

    return removed


def process_due(db_path: Path, deliver, limit: int = 20, uploads_dir: Path | None = None) -> int:
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
            committed = finish(db_path, job["id"], lease_until=lease)
            if committed and job.get("kind") in ("file", "info"):
                try:
                    perform_job_cleanup(db_path, job["id"], uploads_dir=uploads_dir)
                except Exception:
                    logging.exception("Immediate cleanup failed for job %s", job["id"])
        processed += 1

    try:
        process_due_cleanups(db_path, uploads_dir=uploads_dir, limit=limit)
    except Exception:
        logging.exception("Background cleanup sweep failed")

    return processed


def worker_loop(
    db_path: Path,
    deliver,
    stop: threading.Event,
    interval: float = 2.0,
    uploads_dir: Path | None = None,
) -> None:
    last_temp_cleanup = 0.0
    while not stop.is_set():
        try:
            process_due(db_path, deliver, uploads_dir=uploads_dir)
            now = time.time()
            if now - last_temp_cleanup > 300:
                last_temp_cleanup = now
                udir = uploads_dir or get_uploads_dir()
                cleanup_orphaned_temporary_files(udir, db_path)
        except Exception:
            logging.exception("Delivery worker iteration failed")
        stop.wait(interval)
