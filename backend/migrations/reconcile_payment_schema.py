"""
PhotoDoc AI — Schema Reconciliation Migration Tool
Idempotently reconciles existing SQLite databases with the canonical PhotoDoc schema.

Safely handles:
1. Legacy schemas (missing payment fields)
2. Production partial schemas (e.g. missing only order_amount and created_at)
3. Already complete schemas (no-op)

Never deletes columns, never alters existing rows or values.
Supports --check (default/dry-run) and --apply modes.
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Canonical list of expected columns beyond minimal legacy id, name, phone
# Map of column_name -> SQL type / default specification
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
    """Return list of existing column names for the given table in lowercase."""
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    if not cursor.fetchone():
        raise RuntimeError(f"Table '{table_name}' does not exist in database.")

    cursor.execute(f"PRAGMA table_info({table_name})")
    rows = cursor.fetchall()
    return [row[1].lower() for row in rows]


def reconcile_schema(
    db_path: str | Path,
    apply: bool = False,
) -> Dict[str, object]:
    """
    Inspects orders table in db_path.
    If apply is False (check mode):
        Identifies missing columns without mutating DB.
    If apply is True:
        Adds missing columns via ALTER TABLE orders ADD COLUMN ...

    Returns structured result dict.
    """
    db_file = Path(db_path)
    if not db_file.exists():
        raise FileNotFoundError(f"Database file not found: {db_file}")

    conn = sqlite3.connect(str(db_file))
    try:
        existing_cols = get_existing_columns(conn, "orders")
        missing = [col for col in EXPECTED_COLUMNS if col.lower() not in existing_cols]

        if not apply:
            status = "COMPLETE" if not missing else "PARTIAL"
            return {
                "status": status,
                "missing_columns": missing,
                "columns_added": [],
                "migration_required": len(missing) > 0,
                "total_expected": len(EXPECTED_COLUMNS),
                "existing_count": len(existing_cols),
            }

        # Apply mode
        added = []
        if missing:
            cursor = conn.cursor()
            for col in missing:
                col_type = EXPECTED_COLUMNS[col]
                sql = f"ALTER TABLE orders ADD COLUMN {col} {col_type};"
                cursor.execute(sql)
                added.append(col)
            conn.commit()

            # Verify schema after migration
            after_cols = get_existing_columns(conn, "orders")
            still_missing = [col for col in EXPECTED_COLUMNS if col.lower() not in after_cols]
            if still_missing:
                raise RuntimeError(f"Columns still missing after migration: {still_missing}")

            # Verify integrity
            cursor.execute("PRAGMA integrity_check;")
            integrity = cursor.fetchone()
            if not integrity or integrity[0] != "ok":
                raise RuntimeError(f"Integrity check failed: {integrity}")

        return {
            "status": "COMPLETE",
            "missing_columns": [],
            "columns_added": added,
            "migration_required": False,
            "total_expected": len(EXPECTED_COLUMNS),
            "existing_count": len(get_existing_columns(conn, "orders")),
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile PhotoDoc orders table schema")
    parser.add_argument(
        "--db",
        default=os.getenv("DATABASE_PATH", "/data/orders.db"),
        help="Path to SQLite database file",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        default=False,
        help="Check schema without applying changes (default)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Apply missing column migrations",
    )

    args = parser.parse_args()
    db_path = Path(args.db)

    # Safe default: unless --apply is explicitly specified, run in check mode
    apply_mode = args.apply

    try:
        result = reconcile_schema(db_path, apply=apply_mode)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if not apply_mode:
        print(f"SCHEMA_STATUS={result['status']}")
        print(f"MISSING_COLUMNS={','.join(result['missing_columns'])}")
        print(f"MIGRATION_REQUIRED={'true' if result['migration_required'] else 'false'}")
    else:
        if result["columns_added"]:
            print(f"COLUMNS_ADDED={','.join(result['columns_added'])}")
        print(f"SCHEMA_STATUS={result['status']}")
        print(f"MIGRATION_REQUIRED={'true' if result['migration_required'] else 'false'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
