"""Time-based retention purge for customer image files.

Customer source files must not outlive their purpose. Two paths already reduce the
window:

* ``delivery.perform_job_cleanup`` removes a file right after a verified delivery;
* this module removes files of orders that never reached delivery, once the retention
  window (72 hours by default) has passed.

Safety rules, in order of precedence:

1. Only jobs of kind ``file``/``info`` that are **not** delivered are candidates here —
   delivered files belong to the delivery cleanup path.
2. A file is never unlinked when its order is paid: the delivery obligation outlives the
   retention window, so the file is *held* and recorded as such for manual handling.
3. The path must resolve inside the uploads directory, without ``..`` or symlinks, and the
   stored name must carry the owning order id prefix.
4. An intent row is committed before the unlink, and the outcome is recorded afterwards,
   so a crash between the two steps is auditable and repeatable.
5. Running the purge twice is safe: a missing file simply records nothing new.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_HOURS = int(os.getenv("RETENTION_HOURS", "72"))

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
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _record(conn: sqlite3.Connection, job_id: str | None, order_id: int | None, action: str,
            *, reason: str = "", size: int | None = None, path: str = "", now: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO retention_audit "
        "(job_id, order_id, action, reason, bytes, path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job_id, order_id, action, reason, size, path, now),
    )


def _resolve_inside(path: str, uploads_resolved: Path) -> Path | None:
    """Return the resolved path when it is safely inside the uploads directory."""
    try:
        candidate = Path(path)
        if candidate.is_symlink():
            return None
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return None
    if resolved != uploads_resolved and uploads_resolved not in resolved.parents:
        return None
    if ".." in Path(path).parts:
        return None
    return resolved


def purge_expired(
    db_path: Path,
    uploads_dir: Path | None = None,
    *,
    now: datetime | None = None,
    ttl_hours: int | None = None,
    dry_run: bool = False,
    orphan_scan: bool = False,
) -> dict[str, Any]:
    """Delete customer files older than the retention window; return a summary.

    ``dry_run`` reports what would happen without touching the filesystem. ``orphan_scan``
    also removes files in the uploads directory that no job references (disabled by default
    so acceptance fixtures and operator files are never touched accidentally).
    """
    from delivery import get_uploads_dir

    uploads = Path(uploads_dir) if uploads_dir is not None else get_uploads_dir()
    uploads_resolved = uploads.resolve()
    hours = DEFAULT_RETENTION_HOURS if ttl_hours is None else ttl_hours
    current = now or datetime.now(timezone.utc)
    cutoff = _iso(current - timedelta(hours=hours))
    now_ts = _iso(current)

    summary: dict[str, Any] = {
        "retention_hours": hours,
        "cutoff": cutoff,
        "dry_run": dry_run,
        "scanned": 0,
        "deleted": 0,
        "bytes_freed": 0,
        "held_paid": 0,
        "skipped_missing": 0,
        "skipped_unsafe": 0,
        "skipped_mismatch": 0,
        "orphans_deleted": 0,
        "errors": [],
    }

    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(RETENTION_SCHEMA)

        rows = conn.execute(
            "SELECT j.id, j.order_id, j.local_path, j.created_at, j.state, o.payment_status "
            "FROM delivery_jobs j LEFT JOIN orders o ON o.id = j.order_id "
            "WHERE j.kind IN ('file','info') AND j.local_path IS NOT NULL "
            "  AND j.state != 'delivered' AND j.created_at < ?",
            (cutoff,),
        ).fetchall()
        summary["scanned"] = len(rows)

        for row in rows:
            path = row["local_path"]
            if not path:
                continue
            resolved = _resolve_inside(path, uploads_resolved)
            if resolved is None:
                summary["skipped_unsafe"] += 1
                logger.warning("retention: refusing path outside uploads for job %s", row["id"])
                continue
            if not resolved.name.startswith(f"{row['order_id']}_"):
                summary["skipped_mismatch"] += 1
                logger.warning("retention: filename does not carry order id for job %s", row["id"])
                continue
            if not resolved.exists():
                summary["skipped_missing"] += 1
                continue

            if row["payment_status"] == "paid":
                # The delivery obligation prevails: keep the file, flag it for a human.
                summary["held_paid"] += 1
                if not dry_run:
                    _record(conn, row["id"], row["order_id"], "held_paid",
                            reason="paid_undelivered", size=resolved.stat().st_size,
                            path=str(resolved), now=now_ts)
                    conn.commit()
                continue

            size = resolved.stat().st_size
            if dry_run:
                summary["deleted"] += 1
                summary["bytes_freed"] += size
                continue

            try:
                _record(conn, row["id"], row["order_id"], "deleting",
                        reason="retention_expired", size=size, path=str(resolved), now=now_ts)
                conn.commit()
                resolved.unlink()
                _record(conn, row["id"], row["order_id"], "deleted",
                        reason="retention_expired", size=size, path=str(resolved), now=now_ts)
                conn.commit()
                summary["deleted"] += 1
                summary["bytes_freed"] += size
                logger.info("retention: removed expired file for job %s (%d bytes)", row["id"], size)
            except OSError as exc:
                summary["errors"].append(f"{row['id']}: {exc}")
                logger.error("retention: failed to remove file for job %s: %s", row["id"], exc)

        if orphan_scan and uploads_resolved.is_dir():
            referenced = {
                str(Path(item["local_path"]).resolve())
                for item in conn.execute(
                    "SELECT local_path FROM delivery_jobs WHERE local_path IS NOT NULL"
                ).fetchall()
            }
            for entry in uploads_resolved.rglob("*"):
                if not entry.is_file() or entry.is_symlink():
                    continue
                # Never touch operator fixtures or acceptance artefacts, wherever they sit.
                relative = entry.relative_to(uploads_resolved)
                if any(part.startswith("acceptance_") for part in relative.parts):
                    continue
                if str(entry.resolve()) in referenced:
                    continue
                if datetime.fromtimestamp(entry.stat().st_mtime, timezone.utc) >= current - timedelta(hours=hours):
                    continue
                summary["orphans_deleted"] += 1
                if dry_run:
                    continue
                try:
                    size = entry.stat().st_size
                    _record(conn, None, None, f"orphan_deleted:{entry.name}",
                            reason="unreferenced", size=size, path=str(entry), now=now_ts)
                    conn.commit()
                    entry.unlink()
                except OSError as exc:
                    summary["errors"].append(f"orphan {entry.name}: {exc}")

        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    return summary


def main() -> int:
    """CLI entry point: ``python retention_purge.py [--dry-run] [--orphans]``."""
    import argparse

    parser = argparse.ArgumentParser(description="Delete customer files past the retention window")
    parser.add_argument("--db", default=os.getenv("DATABASE_PATH", "/data/orders.db"))
    parser.add_argument("--uploads", default=os.getenv("UPLOAD_DIR", "/app/uploads"))
    parser.add_argument("--hours", type=int, default=DEFAULT_RETENTION_HOURS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--orphans", action="store_true", help="also remove unreferenced old files")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = purge_expired(Path(args.db), Path(args.uploads), ttl_hours=args.hours,
                           dry_run=args.dry_run, orphan_scan=args.orphans)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
