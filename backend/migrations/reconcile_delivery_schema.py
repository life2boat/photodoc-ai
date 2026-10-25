"""P1 additive migration. Apply only to a backed-up PhotoDoc database with the writer stopped."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db_contract import APPLICATION_ID, inspect_database  # noqa: E402
from delivery import DELIVERY_SCHEMA  # noqa: E402


def reconcile(db_path: Path, apply: bool = False) -> dict:
    ready, details = inspect_database(db_path, require_complete_schema=True)
    if not ready or details["application_id"] != APPLICATION_ID:
        raise RuntimeError("P0 PhotoDoc database identity/schema check failed")
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
        table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_jobs'").fetchone()
        indexes = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='delivery_jobs'"
        )}
        needed = ("order_token_hash" not in columns or table is None or
                  not {"idx_delivery_due", "idx_paid_email_once", "idx_admin_email_once"} <= indexes)
        if apply and needed:
            conn.execute("BEGIN IMMEDIATE")
            if "order_token_hash" not in columns:
                conn.execute("ALTER TABLE orders ADD COLUMN order_token_hash TEXT")
            for statement in DELIVERY_SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
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
    args = parser.parse_args()
    result = reconcile(args.db, args.apply)
    print(f"MIGRATION_REQUIRED={str(result['migration_required']).lower()}")
    print(f"APPLIED={str(result['applied']).lower()}")
