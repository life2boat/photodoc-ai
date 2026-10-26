"""Shared SQLite identity and schema contract for PhotoDoc AI."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.parse import quote


# Big-endian ASCII "PDAI". SQLite application_id is a signed 32-bit integer.
APPLICATION_ID_TAG = "PDAI"
APPLICATION_ID = int.from_bytes(APPLICATION_ID_TAG.encode("ascii"), "big")

EXPECTED_ORDERS_COLUMNS: Tuple[str, ...] = (
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
)


def read_application_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA application_id;").fetchone()
    if not row:
        raise RuntimeError("Unable to read SQLite application identity")
    return int(row[0])


def readonly_database_uri(db_path: Path) -> str:
    resolved = db_path.resolve().as_posix()
    return f"file:{quote(resolved, safe='/:')}?mode=ro"


def inspect_database(
    db_path: str | Path,
    *,
    allow_legacy_unmarked: bool = False,
    require_complete_schema: bool = True,
) -> Tuple[bool, Dict[str, object]]:
    """Inspect DB metadata read-only and without querying customer rows."""
    path = Path(db_path)
    details: Dict[str, object] = {
        "db_exists": False,
        "db_nonzero": False,
        "db_readable": False,
        "application_id": None,
        "identity_valid": False,
        "legacy_unmarked": False,
        "orders_table_present": False,
        "schema_readable": False,
        "columns_found": [],
        "missing_columns": list(EXPECTED_ORDERS_COLUMNS),
        "ready_for_migration": False,
        "ready_for_startup": False,
    }

    if not path.exists() or not path.is_file():
        return False, details
    details["db_exists"] = True

    try:
        if path.stat().st_size <= 0:
            return False, details
    except OSError:
        return False, details
    details["db_nonzero"] = True

    try:
        conn = sqlite3.connect(readonly_database_uri(path), uri=True, timeout=5.0)
    except Exception:
        return False, details

    try:
        details["db_readable"] = True
        application_id = read_application_id(conn)
        details["application_id"] = application_id
        details["identity_valid"] = application_id == APPLICATION_ID
        details["legacy_unmarked"] = application_id == 0

        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='orders';"
        ).fetchone()
        if not table:
            return False, details
        details["orders_table_present"] = True

        rows = conn.execute("PRAGMA table_info(orders);").fetchall()
        if not rows:
            return False, details
        columns: List[str] = [str(row[1]).lower() for row in rows]
        missing = [column for column in EXPECTED_ORDERS_COLUMNS if column not in columns]
        details["schema_readable"] = True
        details["columns_found"] = columns
        details["missing_columns"] = missing

        identity_accepted = application_id == APPLICATION_ID or (
            allow_legacy_unmarked and application_id == 0
        )
        details["ready_for_migration"] = bool(identity_accepted)
        details["ready_for_startup"] = bool(
            application_id == APPLICATION_ID and not missing
        )
        success = (
            details["ready_for_startup"]
            if require_complete_schema
            else details["ready_for_migration"]
        )
        return bool(success), details
    except Exception:
        return False, details
    finally:
        conn.close()
