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
import sys
from pathlib import Path
from typing import Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db_contract import APPLICATION_ID, inspect_database  # noqa: E402

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


def verify_database(
    db_path: Path, *, allow_legacy_unmarked: bool = False
) -> Tuple[bool, Dict[str, object]]:
    """
    Performs strictly read-only validation of candidate database.
    Returns (success_bool, details_dict).
    """
    return inspect_database(
        db_path,
        allow_legacy_unmarked=allow_legacy_unmarked,
        require_complete_schema=not allow_legacy_unmarked,
    )


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
    parser.add_argument(
        "--allow-legacy-unmarked",
        action="store_true",
        help="Explicit one-time pre-migration acceptance of application_id=0",
    )

    args = parser.parse_args()

    overall_success = True

    # Database verification
    if args.db:
        db_path = Path(args.db)
        db_success, db_details = verify_database(
            db_path, allow_legacy_unmarked=args.allow_legacy_unmarked
        )
        print(f"DB_EXISTS={'true' if db_details['db_exists'] else 'false'}")
        print(f"DB_NONZERO={'true' if db_details['db_nonzero'] else 'false'}")
        print(f"DB_READABLE={'true' if db_details['db_readable'] else 'false'}")
        print(f"ORDERS_TABLE_PRESENT={'true' if db_details['orders_table_present'] else 'false'}")
        print(f"SCHEMA_READABLE={'true' if db_details['schema_readable'] else 'false'}")
        application_id = db_details["application_id"]
        print(f"DB_APPLICATION_ID={'' if application_id is None else application_id}")
        print(f"EXPECTED_DB_APPLICATION_ID={APPLICATION_ID}")
        print(f"DB_IDENTITY_VALID={'true' if db_details['identity_valid'] else 'false'}")
        print(f"LEGACY_UNMARKED={'true' if db_details['legacy_unmarked'] else 'false'}")
        missing_str = ",".join(db_details["missing_columns"]) if db_details["missing_columns"] else ""
        print(f"MISSING_COLUMNS={missing_str}")
        print(f"READY_FOR_MIGRATION={'true' if db_details['ready_for_migration'] else 'false'}")
        print(f"READY_FOR_STARTUP={'true' if db_details['ready_for_startup'] else 'false'}")
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
