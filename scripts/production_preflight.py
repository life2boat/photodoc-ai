#!/usr/bin/env python3
"""
PhotoDoc AI — Production Database & Environment Preflight Verification Tool

Strictly read-only preflight tool that validates candidate database files and
environment configuration before cutover to prevent deploying against missing,
empty, or unreadable databases, or missing production credentials.

- Never queries order rows, customer records, or any PII.
- Strictly read-only: uses URI mode=ro exclusively (no writable connection fallback).
- Never echoes secret values from environment files.
- Fails closed with non-zero exit code if database, schema, or required credentials are missing.
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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

# Required backend environment variable names
REQUIRED_ENV_VARS: List[str] = [
    "ROBOKASSA_MERCHANT_LOGIN",
    "ROBOKASSA_PASSWORD1",
    "ROBOKASSA_PASSWORD2",
    "ROBOKASSA_IS_TEST",
    "SMTP_SERVER",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "EMAIL_TO",
    "YANDEX_DISK_TOKEN",
]


def verify_database(db_path: Path) -> Tuple[bool, Dict[str, object]]:
    """
    Performs strictly read-only validation of candidate database.
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

    # Read-only SQLite connection URI strictly — no writable fallback
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except Exception:
        # Strictly fail closed if read-only connection fails
        return False, details

    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='orders';"
        )
        row = cursor.fetchone()
        if not row:
            return False, details

        details["orders_table_present"] = True

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


def verify_env_file(env_path: Path) -> Tuple[bool, Dict[str, str]]:
    """
    Verifies that required environment variable names exist in the given file.
    NEVER outputs or logs secret values.
    Returns (all_present_bool, {var_name: 'PRESENT'|'MISSING'}).
    """
    results: Dict[str, str] = {var: "MISSING" for var in REQUIRED_ENV_VARS}

    if not env_path.exists() or not env_path.is_file():
        return False, results

    try:
        content = env_path.read_text(encoding="utf-8")
    except Exception:
        return False, results

    present_keys = set()
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            # If key is in required and has non-empty value
            if key in REQUIRED_ENV_VARS:
                if val != "":
                    present_keys.add(key)
                else:
                    # Explicit empty value
                    pass

    for var in REQUIRED_ENV_VARS:
        if var in present_keys:
            results[var] = "PRESENT"
        else:
            results[var] = "MISSING"

    all_present = all(status == "PRESENT" for status in results.values())
    return all_present, results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify candidate production database and environment before cutover"
    )
    parser.add_argument(
        "--db",
        required=False,
        default=None,
        help="Path to candidate SQLite database file",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        default=True,
        help="Perform read-only verification without modifying data (default)",
    )
    parser.add_argument(
        "--check-env",
        required=False,
        default=None,
        help="Path to backend environment file to verify variable names presence",
    )

    args = parser.parse_args()

    overall_success = True

    # Database verification
    if args.db:
        db_path = Path(args.db)
        db_success, db_details = verify_database(db_path)
        print(f"DB_EXISTS={'true' if db_details['db_exists'] else 'false'}")
        print(f"DB_NONZERO={'true' if db_details['db_nonzero'] else 'false'}")
        print(f"ORDERS_TABLE_PRESENT={'true' if db_details['orders_table_present'] else 'false'}")
        print(f"SCHEMA_READABLE={'true' if db_details['schema_readable'] else 'false'}")
        missing_str = ",".join(db_details["missing_columns"]) if db_details["missing_columns"] else ""
        print(f"MISSING_COLUMNS={missing_str}")
        print(f"READY_FOR_MIGRATION={'true' if db_details['ready_for_migration'] else 'false'}")
        if not db_success:
            overall_success = False

    # Environment verification
    if args.check_env:
        env_path = Path(args.check_env)
        env_success, env_details = verify_env_file(env_path)
        for var in REQUIRED_ENV_VARS:
            print(f"{var}={env_details[var]}")
        print("ENV_SECRET_VALUES_PRINTED=false")
        print(f"READY_FOR_CUTOVER={'true' if env_success else 'false'}")
        if not env_success:
            overall_success = False

    if not args.db and not args.check_env:
        print("ERROR: Specify --db, --check-env, or both.", file=sys.stderr)
        return 1

    return 0 if overall_success else 1


if __name__ == "__main__":
    sys.exit(main())
