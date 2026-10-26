#!/usr/bin/env python3
"""Atomically reconcile the PhotoDoc SQLite schema and application identity."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db_contract import (  # noqa: E402
    APPLICATION_ID,
    EXPECTED_ORDERS_COLUMNS,
    read_application_id,
)


EXPECTED_COLUMNS: Dict[str, str] = {
    "email": "TEXT",
    "format": "TEXT",
    "paper": "TEXT",
    "crop": "TEXT",
    "comment": "TEXT",
    "filename": "TEXT",
    "status": "TEXT DEFAULT 'new'",
    "payment_status": "TEXT DEFAULT 'unpaid'",
    "order_amount": "TEXT",
    "payment_amount": "TEXT",
    "payment_method": "TEXT DEFAULT 'robokassa'",
    "robokassa_inv_id": "TEXT",
    "paid_at": "TEXT",
    "payment_email_sent_at": "TEXT",
    "created_at": "TEXT DEFAULT NULL",
}


def get_existing_columns(conn: sqlite3.Connection, table_name: str = "orders") -> List[str]:
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone()
    if not table:
        raise RuntimeError(f"Table '{table_name}' does not exist in database.")
    return [str(row[1]).lower() for row in conn.execute(f"PRAGMA table_info({table_name})")]


def _validate_source_identity(application_id: int, allow_legacy_unmarked: bool) -> None:
    if application_id == APPLICATION_ID:
        return
    if application_id == 0 and allow_legacy_unmarked:
        return
    if application_id == 0:
        raise RuntimeError(
            "Database is legacy/unmarked; pass --allow-legacy-unmarked only during the "
            "controlled production reconciliation"
        )
    raise RuntimeError("Database application_id belongs to another application")


def reconcile_schema(
    db_path: str | Path,
    apply: bool = False,
    *,
    allow_legacy_unmarked: bool = False,
    failure_after_columns: int | None = None,
) -> Dict[str, object]:
    """Inspect or atomically reconcile schema plus the PhotoDoc application_id."""
    db_file = Path(db_path)
    if not db_file.exists() or not db_file.is_file():
        raise FileNotFoundError(f"Database file not found: {db_file}")
    if db_file.stat().st_size <= 0:
        raise RuntimeError("Database file is empty")

    conn = sqlite3.connect(str(db_file), timeout=10.0)
    try:
        application_id_before = read_application_id(conn)
        _validate_source_identity(application_id_before, allow_legacy_unmarked)
        existing_cols = get_existing_columns(conn, "orders")
        missing = [column for column in EXPECTED_COLUMNS if column not in existing_cols]

        if not apply:
            return {
                "status": "COMPLETE" if not missing and application_id_before == APPLICATION_ID else "PARTIAL",
                "missing_columns": missing,
                "columns_added": [],
                "migration_required": bool(missing or application_id_before != APPLICATION_ID),
                "total_expected": len(EXPECTED_ORDERS_COLUMNS),
                "existing_count": len(existing_cols),
                "application_id": application_id_before,
            }

        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        added: list[str] = []
        try:
            for column in missing:
                cursor.execute(
                    f"ALTER TABLE orders ADD COLUMN {column} {EXPECTED_COLUMNS[column]};"
                )
                added.append(column)
                if failure_after_columns is not None and len(added) >= failure_after_columns:
                    raise RuntimeError("Injected migration failure")

            cursor.execute(f"PRAGMA application_id = {APPLICATION_ID}")

            after_cols = get_existing_columns(conn, "orders")
            still_missing = [
                column for column in EXPECTED_ORDERS_COLUMNS if column not in after_cols
            ]
            if still_missing:
                raise RuntimeError(f"Columns still missing after migration: {still_missing}")
            if read_application_id(conn) != APPLICATION_ID:
                raise RuntimeError("Application identity was not applied")

            integrity = cursor.execute("PRAGMA integrity_check;").fetchone()
            if not integrity or integrity[0] != "ok":
                raise RuntimeError(f"Integrity check failed: {integrity}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return {
            "status": "COMPLETE",
            "missing_columns": [],
            "columns_added": added,
            "migration_required": False,
            "total_expected": len(EXPECTED_ORDERS_COLUMNS),
            "existing_count": len(get_existing_columns(conn, "orders")),
            "application_id": read_application_id(conn),
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.getenv("DATABASE_PATH", "/data/orders.db"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Read-only inspection (default)")
    mode.add_argument("--apply", action="store_true", help="Apply reconciliation atomically")
    parser.add_argument(
        "--allow-legacy-unmarked",
        action="store_true",
        help="Explicitly accept application_id=0 for the one-time controlled migration",
    )
    args = parser.parse_args()

    try:
        result = reconcile_schema(
            args.db,
            apply=args.apply,
            allow_legacy_unmarked=args.allow_legacy_unmarked,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"SCHEMA_STATUS={result['status']}")
    print(f"MISSING_COLUMNS={','.join(result['missing_columns'])}")
    if args.apply and result["columns_added"]:
        print(f"COLUMNS_ADDED={','.join(result['columns_added'])}")
    print(f"DB_APPLICATION_ID={result['application_id']}")
    print(f"MIGRATION_REQUIRED={'true' if result['migration_required'] else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
