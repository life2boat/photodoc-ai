"""Time-based retention purge for customer image files.

Safety model
------------
Deleting a file and confirming a payment are two different systems (SQLite and the
filesystem), so this module never claims atomicity between them. Instead it uses a
*reversible* deletion step:

1. The order is read with an inner join; a missing row or any status other than
   ``unpaid`` blocks deletion (``pending`` may still be confirmed by the provider).
2. The file identity (sha256 + size) is verified against the job metadata.
3. A hard link to the same inode is created in ``uploads/.quarantine`` — the bytes
   survive the unlink that follows, so a mistake can be undone.
4. ``Path.unlink`` removes the visible name.
5. Immediately afterwards the order is re-read:
   * if the payment arrived (``pending``/``paid``) or the order disappeared, the file is
     restored from the link and the event is recorded as ``restored_after_payment``;
   * if the bytes changed between the check and the unlink (substitution), the current
     content is restored and recorded as ``restored_after_substitution``;
   * otherwise the quarantine link is removed and the deletion is recorded as
     ``deleted``.
6. A crash between the unlink and the final record leaves the ``deleting`` intent plus a
   quarantine link; the next run reconciles it (restore or finish) instead of reporting a
   silent ``missing``.

Concurrency: workers may race on the same file. Exactly one wins the unlink; the others
see ``FileNotFoundError`` and reconcile against the audit table without reporting an error.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_HOURS = int(os.getenv("RETENTION_HOURS", "72"))
QUARANTINE_DIR = ".quarantine"
DELETABLE_ORPHAN_SUFFIXES = (".part", ".tmp", ".upload")

RETENTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS retention_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    order_id INTEGER,
    action TEXT NOT NULL,
    reason TEXT,
    bytes INTEGER,
    path TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(job_id, action)
);
CREATE INDEX IF NOT EXISTS idx_retention_created ON retention_audit(created_at);
CREATE TABLE IF NOT EXISTS retention_claims (
    job_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    claimed_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_ts(value: Any) -> datetime | None:
    """Parse a stored timestamp, assuming UTC for naive legacy values."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def _looks_like_sha256(value: Any) -> bool:
    """Only a real digest can be used for identity; legacy placeholders are ignored."""
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _record(conn: sqlite3.Connection, job_id: str | None, order_id: int | None, action: str,
            *, reason: str = "", size: int | None = None, path: str = "", now: str) -> bool:
    """Insert an audit row; True only when this call created it (UNIQUE per job/action)."""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO retention_audit "
        "(job_id, order_id, action, reason, bytes, path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job_id, order_id, action, reason, size, path, now),
    )
    return cursor.rowcount == 1


def _has_action(conn: sqlite3.Connection, job_id: str, action: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM retention_audit WHERE job_id=? AND action=?", (job_id, action)
    ).fetchone()
    return row is not None


def _wait_for_peer_deletion(conn: sqlite3.Connection, job_id: str, timeout: float = 2.0) -> bool:
    """Give a peer worker a moment to record its completed deletion."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        conn.commit()
        if _has_action(conn, job_id, "deleted"):
            return True
        time.sleep(0.05)
    conn.commit()
    return _has_action(conn, job_id, "deleted")


def _resolve_inside(path: str, uploads_resolved: Path) -> Path | None:
    try:
        candidate = Path(path)
        if ".." in candidate.parts:
            return None
        if candidate.is_symlink():
            return None
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return None
    if resolved != uploads_resolved and uploads_resolved not in resolved.parents:
        return None
    if resolved.parent != uploads_resolved:
        return None
    return resolved


def _order_status(db_path: Path, order_id: int) -> str | None:
    """Read the payment status in its own connection; None means the order is unknown."""
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        row = conn.execute("SELECT payment_status FROM orders WHERE id=?", (order_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _restore(quarantine: Path, target: Path) -> bool:
    try:
        os.link(quarantine, target)
        os.unlink(quarantine)
        return True
    except OSError as exc:
        logger.error("retention: could not restore %s from quarantine: %s", target, exc)
        return False


def _delete_quarantine(quarantine: Path) -> bool:
    try:
        quarantine.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("retention: quarantine cleanup failed for %s: %s", quarantine, exc)
        return False
    return True


def _handle_interrupted(conn: sqlite3.Connection, uploads: Path, now_iso: str) -> dict[str, int]:
    """Reconcile intents left by a previous run that never recorded an outcome."""
    stats = {"recovered_deleted": 0, "recovered_restored": 0, "recovered_uncertain": 0}
    rows = conn.execute(
        "SELECT a.job_id, a.order_id, a.path FROM retention_audit a "
        "WHERE a.action='deleting' AND NOT EXISTS ("
        "  SELECT 1 FROM retention_audit b WHERE b.job_id=a.job_id "
        "  AND b.action IN ('deleted','restored_after_payment','restored_after_substitution','uncertain'))"
    ).fetchall()
    quarantine_dir = uploads / QUARANTINE_DIR
    for job_id, order_id, path in rows:
        candidates = sorted(quarantine_dir.glob(f"{job_id}.*.claim")) if quarantine_dir.is_dir() else []
        quarantine = candidates[0] if candidates else None
        target = Path(path) if path else None
        status = _order_status(conn_path_for(conn), order_id) if order_id is not None else None
        if quarantine is not None and quarantine.exists():
            if target is not None and target.exists():
                # The unlink never ran (crash before it); the normal pass will retry.
                _delete_quarantine(quarantine)
                continue
            if status is not None and status != "unpaid":
                if target is not None and _restore(quarantine, target):
                    _record(conn, job_id, order_id, "restored_after_payment",
                            reason="interrupted_deletion_payment_arrived", path=str(target), now=now_iso)
                    stats["recovered_restored"] += 1
                    continue
                _record(conn, job_id, order_id, "recovery_required",
                        reason="interrupted_deletion_payment_restore_failed", path=str(target or ""), now=now_iso)
                stats["recovered_recovery_required"] = stats.get("recovered_recovery_required", 0) + 1
                continue
            _record(conn, job_id, order_id, "deleted",
                    reason="interrupted_deletion_finalised", path=str(target or ""), now=now_iso)
            stats["recovered_deleted"] += 1
            continue
        if target is not None and target.exists():
            # The unlink never ran; the next normal pass will retry it.
            continue
        _record(conn, job_id, order_id, "uncertain",
                reason="quarantine_missing_and_file_absent", path=str(target or ""), now=now_iso)
        stats["recovered_uncertain"] += 1
    conn.commit()
    return stats


def conn_path_for(conn: sqlite3.Connection) -> Path:
    """Best-effort database path for a connection opened on a file."""
    row = conn.execute("PRAGMA database_list").fetchone()
    return Path(row[2]) if row and row[2] else Path(":memory:")


def purge_expired(
    db_path: Path,
    uploads_dir: Path | None = None,
    *,
    now: datetime | None = None,
    ttl_hours: int | None = None,
    dry_run: bool = False,
    orphan_scan: bool = False,
) -> dict[str, Any]:
    """Delete customer files older than the retention window; return a summary."""
    from delivery import get_uploads_dir

    uploads = Path(uploads_dir) if uploads_dir is not None else get_uploads_dir()
    uploads_resolved = uploads.resolve()
    hours = DEFAULT_RETENTION_HOURS if ttl_hours is None else ttl_hours
    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(hours=hours)
    now_iso = _iso(current)

    summary: dict[str, Any] = {
        "retention_hours": hours, "dry_run": dry_run, "scanned": 0, "deleted": 0,
        "bytes_freed": 0, "held_paid": 0, "held_pending": 0, "held_missing_order": 0,
        "restored_after_payment": 0, "restored_after_substitution": 0, "recovery_required": 0,
        "skipped_missing": 0, "skipped_unsafe": 0, "skipped_mismatch": 0,
        "orphans_deleted": 0, "uncertain": 0, "errors": [],
    }
    summary.update({"recovered_deleted": 0, "recovered_restored": 0, "recovered_uncertain": 0})

    if dry_run:
        return _dry_run(db_path, uploads_resolved, current, cutoff, summary, orphan_scan)

    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(RETENTION_SCHEMA)
        conn.commit()
        summary.update(_handle_interrupted(conn, uploads_resolved, now_iso))

        rows = conn.execute(
            "SELECT j.id, j.order_id, j.local_path, j.created_at, j.state, j.content_sha256, "
            "j.content_size, o.payment_status "
            "FROM delivery_jobs j JOIN orders o ON o.id = j.order_id "
            "WHERE j.kind IN ('file','info') AND j.local_path IS NOT NULL "
            "  AND j.state != 'delivered'"
        ).fetchall()

        quarantine_dir = uploads_resolved / QUARANTINE_DIR
        for row in rows:
            created = _parse_ts(row["created_at"])
            if created is None or not created < cutoff:
                continue
            summary["scanned"] += 1
            job_id, order_id, path = row["id"], row["order_id"], row["local_path"]
            if _has_action(conn, job_id, "deleted"):
                continue

            status = row["payment_status"]
            if status == "paid":
                summary["held_paid"] += 1
                _record(conn, job_id, order_id, "held_paid", reason="paid_undelivered", now=now_iso)
                conn.commit()
                continue
            if status != "unpaid":
                summary["held_pending"] += 1
                _record(conn, job_id, order_id, "held_pending", reason=f"payment_status={status}", now=now_iso)
                conn.commit()
                continue

            resolved = _resolve_inside(path, uploads_resolved)
            if resolved is None:
                summary["skipped_unsafe"] += 1
                continue
            if not resolved.name.startswith(f"{order_id}_"):
                summary["skipped_mismatch"] += 1
                continue
            if not resolved.exists():
                summary["skipped_missing"] += 1
                continue

            expected_sha = row["content_sha256"]
            expected_size = row["content_size"]
            try:
                actual_size = resolved.stat().st_size
                # Hash the visible file *now*; the same hash after the unlink proves the
                # bytes in the quarantine link are the ones we inspected, not a replacement.
                pre_sha = _sha256(resolved)
            except OSError as exc:
                summary["errors"].append(f"{job_id}: {exc}")
                continue
            if expected_size is not None and actual_size != expected_size:
                summary["skipped_mismatch"] += 1
                continue
            if _looks_like_sha256(expected_sha) and pre_sha != expected_sha:
                summary["skipped_mismatch"] += 1
                continue

            quarantine_dir.mkdir(parents=True, exist_ok=True)
            token = uuid.uuid4().hex
            quarantine = quarantine_dir / f"{job_id}.{token[:8]}.claim"
            try:
                os.link(resolved, quarantine)
            except OSError as exc:
                summary["errors"].append(f"{job_id}: quarantine {exc}")
                continue

            # One worker owns the finalisation; the others may still run the unlink (a
            # crashed owner must not stall the queue) but they never double-count it.
            claim = conn.execute(
                "INSERT OR IGNORE INTO retention_claims (job_id, owner, claimed_at) VALUES (?, ?, ?)",
                (job_id, token, now_iso),
            )
            conn.commit()
            is_owner = claim.rowcount == 1
            current_owner = None if is_owner else conn.execute(
                "SELECT owner FROM retention_claims WHERE job_id=?", (job_id,)
            ).fetchone()

            _record(conn, job_id, order_id, "deleting", reason="retention_expired",
                    size=actual_size, path=str(resolved), now=now_iso)
            conn.commit()

            unlink_error: OSError | None = None
            for attempt in range(10):
                try:
                    resolved.unlink()
                    unlink_error = None
                    break
                except FileNotFoundError:
                    unlink_error = None
                    break
                except OSError as exc:
                    # Another worker may still hold a handle on Windows; retry briefly
                    # before treating it as a real failure.
                    unlink_error = exc
                    time.sleep(0.05)
            if unlink_error is not None:
                _delete_quarantine(quarantine)
                summary["errors"].append(f"{job_id}: {unlink_error}")
                continue

            if not is_owner:
                # Another worker may own the outcome; if its claim is stale (crashed
                # process), take it over so the deletion is still finalised exactly once.
                takeover = None
                if current_owner is not None:
                    takeover = conn.execute(
                        "UPDATE retention_claims SET owner=?, claimed_at=? WHERE job_id=? AND owner=?",
                        (token, now_iso, job_id, current_owner[0]),
                    )
                    conn.commit()
                if takeover is None or takeover.rowcount != 1:
                    _delete_quarantine(quarantine)
                    summary["skipped_missing"] += 1
                    continue
                is_owner = True

            after = _order_status(db_path, order_id)
            quarantine_sha = None
            quarantine_size = None
            if quarantine.exists():
                try:
                    quarantine_size = quarantine.stat().st_size
                    quarantine_sha = _sha256(quarantine)
                except OSError:
                    pass

            if after is None or after != "unpaid":
                if resolved.exists() is False and _restore(quarantine, resolved):
                    inserted = _record(conn, job_id, order_id, "restored_after_payment",
                                       reason=f"payment_status={after}", size=quarantine_size,
                                       path=str(resolved), now=now_iso)
                    conn.commit()
                    if inserted:
                        summary["restored_after_payment"] += 1
                    else:
                        summary["skipped_missing"] += 1
                else:
                    # C-01: the quarantine holds the only remaining copy of a paid source.
                    # Dropping it turns a recoverable failure into data loss, so it is kept
                    # and the operator must resolve the state manually.
                    inserted = _record(conn, job_id, order_id, "recovery_required",
                                       reason="payment_arrived_but_restore_failed",
                                       size=quarantine_size, path=str(resolved), now=now_iso)
                    conn.commit()
                    if inserted:
                        summary["recovery_required"] += 1
                continue

            if quarantine_sha is not None and quarantine_sha != pre_sha:
                if not resolved.exists() and _restore(quarantine, resolved):
                    inserted = _record(conn, job_id, order_id, "restored_after_substitution",
                                       reason="content_changed_before_unlink", size=quarantine_size,
                                       path=str(resolved), now=now_iso)
                    conn.commit()
                    if inserted:
                        summary["restored_after_substitution"] += 1
                    else:
                        summary["skipped_missing"] += 1
                else:
                    # Same rule for a substituted file: the quarantined bytes are the only
                    # evidence of what was there, so they are preserved for manual recovery.
                    inserted = _record(conn, job_id, order_id, "recovery_required",
                                       reason="substitution_detected_restore_failed",
                                       size=quarantine_size, path=str(resolved), now=now_iso)
                    conn.commit()
                    if inserted:
                        summary["recovery_required"] += 1
                continue

            # C-02: a payment may still be committed after the last read above. The
            # quarantine is therefore kept and released later by purge_quarantines(), which
            # re-reads the payment state under a write transaction before removing it.
            inserted = _record(conn, job_id, order_id, "deleted", reason="retention_expired",
                               size=quarantine_size, path=str(resolved), now=now_iso)
            conn.commit()
            if inserted:
                summary["deleted"] += 1
                summary["bytes_freed"] += quarantine_size or 0
            else:
                summary["skipped_missing"] += 1

        if orphan_scan and uploads_resolved.is_dir():
            summary["orphans_deleted"] += _purge_orphan_temp_files(
                conn, uploads_resolved, current, hours, now_iso, dry_run=False)
        conn.commit()
    finally:
        conn.close()

    return summary


def _purge_orphan_temp_files(conn: sqlite3.Connection, uploads: Path, current: datetime,
                             hours: int, now_iso: str, dry_run: bool) -> int:
    """Only known temporary suffixes are eligible; customer sources are never guessed."""
    removed = 0
    cutoff = current - timedelta(hours=hours)
    for entry in uploads.rglob("*"):
        if not entry.is_file() or entry.is_symlink():
            continue
        if entry.name.endswith(DELETABLE_ORPHAN_SUFFIXES) is False:
            continue
        try:
            mtime = datetime.fromtimestamp(entry.stat().st_mtime, timezone.utc)
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        if dry_run:
            removed += 1
            continue
        try:
            size = entry.stat().st_size
            entry.unlink()
            removed += 1
            _record(conn, None, None, f"orphan_deleted:{entry.name}", reason="temporary_file",
                    size=size, path=str(entry), now=now_iso)
        except OSError as exc:
            logger.warning("retention: orphan cleanup failed for %s: %s", entry, exc)
    return removed


def _dry_run(db_path: Path, uploads: Path, current: datetime, cutoff: datetime,
             summary: dict[str, Any], orphan_scan: bool) -> dict[str, Any]:
    """Read-only planning: the database must not change, not even its schema."""
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error as exc:
        summary["errors"].append(str(exc))
        return summary
    try:
        try:
            rows = conn.execute(
                "SELECT j.id, j.order_id, j.local_path, j.created_at, j.state, j.content_sha256, "
                "j.content_size, o.payment_status "
                "FROM delivery_jobs j JOIN orders o ON o.id = j.order_id "
                "WHERE j.kind IN ('file','info') AND j.local_path IS NOT NULL AND j.state != 'delivered'"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            summary["errors"].append(str(exc))
            return summary
        for row in rows:
            created = _parse_ts(row[3])
            if created is None or not created < cutoff:
                continue
            summary["scanned"] += 1
            status = row[7]
            if status == "paid":
                summary["held_paid"] += 1
            elif status != "unpaid":
                summary["held_pending"] += 1
            else:
                summary["deleted"] += 1
    finally:
        conn.close()
    if orphan_scan and uploads.is_dir():
        summary["orphans_deleted"] = _purge_orphan_temp_files(None, uploads, current, 0, "", dry_run=True)
    return summary


def main() -> int:
    """CLI entry point: ``python retention.py [--dry-run] [--orphans]``."""
    import argparse

    parser = argparse.ArgumentParser(description="Delete customer files past the retention window")
    parser.add_argument("--db", default=os.getenv("DATABASE_PATH", "/data/orders.db"))
    parser.add_argument("--uploads", default=os.getenv("UPLOAD_DIR", "/app/uploads"))
    parser.add_argument("--hours", type=int, default=DEFAULT_RETENTION_HOURS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--orphans", action="store_true", help="also remove known temporary files")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = purge_expired(Path(args.db), Path(args.uploads), ttl_hours=args.hours,
                           dry_run=args.dry_run, orphan_scan=args.orphans)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

def purge_quarantines(db_path: Path, uploads_dir: Path | None = None, *,
                      now: datetime | None = None, grace_hours: int = 24,
                      dry_run: bool = False) -> dict[str, Any]:
    """Release quarantined copies only after a fresh payment check.

    A file removed by ``purge_expired`` stays in ``uploads/.quarantine`` until this pass runs
    later. If the order became paid in the meantime, the file is restored; otherwise the
    quarantined copy is dropped. The check runs inside a write transaction, so a concurrent
    payment either commits before it (and wins) or after the copy is already gone, which the
    late-callback contract in ``main.mark_order_paid`` reports explicitly.
    """
    from delivery import get_uploads_dir

    uploads = Path(uploads_dir) if uploads_dir is not None else get_uploads_dir()
    current = now or datetime.now(timezone.utc)
    if grace_hours < 0:
        raise ValueError("grace_hours must be >= 0")
    cutoff = current - timedelta(hours=grace_hours)
    quarantine_dir = uploads / ".quarantine"
    summary: dict[str, Any] = {"scanned": 0, "released": 0, "restored": 0,
                               "kept_for_recovery": 0, "errors": []}
    if quarantine_dir.is_symlink() or not quarantine_dir.is_dir():
        return summary

    conn = sqlite3.connect(db_path, timeout=10)
    try:
        for claim in sorted(quarantine_dir.glob("*.claim")):
            summary["scanned"] += 1
            job_id = claim.name.split(".")[0]
            try:
                # Lock order: SQLite writer first, then bounded local filesystem work.
                # Never wait for a payment writer while holding this lock. A payment
                # commits before this transaction (restore), or after unlink (late
                # payment / SourceMissingAtPayment). No network calls in this section.
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT a.order_id, a.path, a.created_at, j.order_id, j.local_path, "
                    "j.kind, j.state, o.payment_status, j.artifact_role FROM retention_audit a "
                    "JOIN delivery_jobs j ON j.id=a.job_id "
                    "JOIN orders o ON o.id=a.order_id "
                    "WHERE a.job_id=? AND a.action='deleted' ORDER BY a.id DESC LIMIT 1",
                    (job_id,),
                ).fetchone()
                if row is None:
                    continue
                order_id, path, created, job_order, job_path, kind, state, status, role = row
                created = _parse_ts(created)
                target = _resolve_inside(path or "", uploads.resolve())
                if (created is None or created > cutoff or target is None
                        or order_id != job_order or kind not in ("file", "info")
                        or state in ("processing", "delivered")
                        or role not in (None, "source", "info")
                        or not target.name.startswith(f"{order_id}_")
                        or _resolve_inside(job_path or "", uploads.resolve()) != target):
                    continue
                # Revalidate physical containment immediately before restore/unlink.
                if (quarantine_dir.is_symlink() or claim.is_symlink()
                        or not claim.is_file() or claim.resolve().parent != quarantine_dir.resolve()):
                    raise ValueError("Unsafe quarantine artifact")
                if status not in ("unpaid", "paid", "pending"):
                    summary["kept_for_recovery"] += 1
                    continue
                if dry_run:
                    summary["released" if status == "unpaid" else "kept_for_recovery"] += 1
                    continue
                if status != "unpaid":
                    if not target.exists() and _restore(claim, target):
                        conn.execute(
                            "INSERT OR IGNORE INTO retention_audit (job_id, order_id, action, reason, path, created_at)"
                            " VALUES (?, ?, 'restored_after_payment', 'quarantine_release', ?, ?)",
                            (job_id, order_id, str(target), current.isoformat()))
                        conn.commit()
                        summary["restored"] += 1
                    else:
                        summary["kept_for_recovery"] += 1
                    continue
                if not _delete_quarantine(claim):
                    raise OSError("Quarantine unlink failed; retained for retry")
                # Release the writer lock ONLY after the irreversible filesystem step.
                conn.commit()
                summary["released"] += 1
            except (sqlite3.Error, OSError, ValueError) as exc:
                summary["errors"].append(f"{job_id}: {exc}")
            finally:
                # Also release on ineligible rows/failure; the next cycle can retry.
                conn.rollback()
    finally:
        conn.close()
    return summary
