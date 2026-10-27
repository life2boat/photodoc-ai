"""P1 additive migration. Apply only to a backed-up PhotoDoc database with the writer stopped."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db_contract import APPLICATION_ID, inspect_database  # noqa: E402
from delivery import DELIVERY_SCHEMA  # noqa: E402

REQUIRED_CLEANUP_COLUMNS = {
    "cleanup_state",
    "local_deleted_at",
    "cleanup_error",
    "cleanup_attempts",
    "cleanup_lease_until",
}


def reconcile(db_path: Path, apply: bool = False, uploads_dir: Path | None = None) -> dict:
    ready, details = inspect_database(db_path, require_complete_schema=True)
    if not ready or details["application_id"] != APPLICATION_ID:
        raise RuntimeError("P0 PhotoDoc database identity/schema check failed")

    if uploads_dir is None:
        env_dir = os.getenv("UPLOAD_DIR")
        if env_dir:
            uploads_dir = Path(env_dir)
        elif (db_path.parent / "uploads").is_dir():
            uploads_dir = db_path.parent / "uploads"
        else:
            uploads_dir = Path("/app/uploads")

    conn = sqlite3.connect(db_path, timeout=10)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
        table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_jobs'").fetchone()
        delivery_cols = {row[1] for row in conn.execute("PRAGMA table_info(delivery_jobs)")} if table else set()
        indexes = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='delivery_jobs'"
        )} if table else set()

        needed = (
            "order_token_hash" not in columns
            or table is None
            or not REQUIRED_CLEANUP_COLUMNS <= delivery_cols
            or not {"idx_delivery_due", "idx_cleanup_due", "idx_paid_email_once", "idx_admin_email_once"} <= indexes
        )

        if apply and needed:
            conn.execute("BEGIN IMMEDIATE")
            if "order_token_hash" not in columns:
                conn.execute("ALTER TABLE orders ADD COLUMN order_token_hash TEXT")

            if table is None:
                for statement in DELIVERY_SCHEMA.split(";"):
                    if statement.strip():
                        conn.execute(statement)
            else:
                if "cleanup_state" not in delivery_cols:
                    conn.execute("ALTER TABLE delivery_jobs ADD COLUMN cleanup_state TEXT NOT NULL DEFAULT 'pending'")
                if "local_deleted_at" not in delivery_cols:
                    conn.execute("ALTER TABLE delivery_jobs ADD COLUMN local_deleted_at TEXT")
                if "cleanup_error" not in delivery_cols:
                    conn.execute("ALTER TABLE delivery_jobs ADD COLUMN cleanup_error TEXT")
                if "cleanup_attempts" not in delivery_cols:
                    conn.execute("ALTER TABLE delivery_jobs ADD COLUMN cleanup_attempts INTEGER NOT NULL DEFAULT 0")
                if "cleanup_lease_until" not in delivery_cols:
                    conn.execute("ALTER TABLE delivery_jobs ADD COLUMN cleanup_lease_until TEXT")

                conn.execute("CREATE INDEX IF NOT EXISTS idx_cleanup_due ON delivery_jobs(state, cleanup_state, cleanup_lease_until)")
                conn.execute("UPDATE delivery_jobs SET cleanup_state='not_applicable' WHERE kind NOT IN ('file', 'info')")

                # Historical backfill: inspect physical filesystem for delivered file/info jobs
                # Never assume delivered implies cleaned or invent local_deleted_at
                delivered_rows = conn.execute(
                    "SELECT id, local_path FROM delivery_jobs "
                    "WHERE state='delivered' AND kind IN ('file', 'info')"
                ).fetchall()
                for job_id, local_path in delivered_rows:
                    file_exists = False
                    if local_path:
                        p = Path(local_path)
                        if p.is_file():
                            file_exists = True
                        elif uploads_dir is not None and (uploads_dir / p.name).is_file():
                            file_exists = True

                    if file_exists:
                        # File physically exists: keep cleanup_state='pending', local_deleted_at=NULL
                        # Cleanup worker will safely verify sha256/size and perform verified cleanup
                        conn.execute(
                            "UPDATE delivery_jobs SET cleanup_state='pending', local_deleted_at=NULL, cleanup_error=NULL WHERE id=?",
                            (job_id,),
                        )
                    else:
                        # File is physically absent: record cleanup_state='completed' without inventing deletion timestamp
                        conn.execute(
                            "UPDATE delivery_jobs SET cleanup_state='completed', local_deleted_at=NULL, cleanup_error=NULL WHERE id=?",
                            (job_id,),
                        )

                for statement in DELIVERY_SCHEMA.split(";"):
                    trimmed = statement.strip()
                    if trimmed.startswith("CREATE INDEX") or trimmed.startswith("CREATE UNIQUE INDEX"):
                        conn.execute(trimmed)

            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError("SQLite integrity check failed")
            conn.commit()
        return {"migration_required": needed, "applied": bool(apply and needed)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--uploads-dir", type=Path, default=None)
    args = parser.parse_args()
    result = reconcile(args.db, args.apply, uploads_dir=args.uploads_dir)
    print(f"MIGRATION_REQUIRED={str(result['migration_required']).lower()}")
    print(f"APPLIED={str(result['applied']).lower()}")
