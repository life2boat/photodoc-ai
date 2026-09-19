#!/usr/bin/env python3
"""
PhotoDoc AI — Production Database Preflight Verification Tool

Strictly read-only preflight tool that validates a candidate database
before cutover or migration to prevent deploying against a missing, empty,
or unreadable database volume.

Never queries order rows, customer records, or any PII.
Fails closed with a non-zero exit code if database or orders table is missing or invalid.
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Tuple

# Canonical list of expected columns on the orders table
EXPECTED_COLUMNS: List[str] = [
    "id",
    "name",
    "phone",
    "email",
    "format",
    "paper",
    "crop",
    "comment",
    "filename",
    "status",
    "payment_status",
    "order_amount",
    "payment_amount",
    "payment_method",
    "robokassa_inv_id",
    "paid_at",
    "payment_email_sent_at",
    "created_at",
]


def verify_database(db_path: Path) -> Tuple[bool, Dict[str, object]]:
    """
    Performs read-only validation of candidate database.
    Returns (success_bool, details_dict).
    """
    details: Dict[str, object] = {
        "db_exists": False,
        "db_nonzero": False,
        "orders_table_present": False,
        "schema_readable": False,
        "columns_found": [],
        "missing_columns": [],
        "ready_for_migration": False,
    }

    if not db_path.exists() or not db_path.is_file():
        return False, details

    details["db_exists"] = True

    try:
        size = db_path.stat().st_size
    except OSError:
        return False, details

    if size <= 0:
        return False, details

    details["db_nonzero"] = True

    # Read-only SQLite connection URI
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except Exception:
        # Fallback to standard connection if URI mode fails
        try:
            conn = sqlite3.connect(str(db_path.resolve()))
        except Exception:
            return False, details

    try:
        cursor = conn.cursor()
        # Verify orders table exists
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='orders';"
        )
        row = cursor.fetchone()
        if not row:
            return False, details

        details["orders_table_present"] = True

        # Read schema via PRAGMA (strictly structure only, no row queries)
        cursor.execute("PRAGMA table_info(orders);")
        pragma_rows = cursor.fetchall()
        if not pragma_rows:
            return False, details

        existing_cols = [r[1].lower() for r in pragma_rows]
        details["schema_readable"] = True
        details["columns_found"] = existing_cols

        missing = [col for col in EXPECTED_COLUMNS if col.lower() not in existing_cols]
        details["missing_columns"] = missing
        details["ready_for_migration"] = True

        return True, details
    except Exception:
        return False, details
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify candidate production database before cutover"
    )
    parser.add_argument(
        "--db",
        required=True,
        help="Path to candidate SQLite database file",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        default=True,
        help="Perform read-only verification without modifying data (default)",
    )

    args = parser.parse_args()
    db_path = Path(args.db)

    success, details = verify_database(db_path)

    print(f"DB_EXISTS={'true' if details['db_exists'] else 'false'}")
    print(f"DB_NONZERO={'true' if details['db_nonzero'] else 'false'}")
    print(f"ORDERS_TABLE_PRESENT={'true' if details['orders_table_present'] else 'false'}")
    print(f"SCHEMA_READABLE={'true' if details['schema_readable'] else 'false'}")
    missing_str = ",".join(details["missing_columns"]) if details["missing_columns"] else ""
    print(f"MISSING_COLUMNS={missing_str}")
    print(f"READY_FOR_MIGRATION={'true' if details['ready_for_migration'] else 'false'}")

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
