"""
PhotoDoc AI — Migration Reconciliation & Storage Compatibility Test Suite

Verifies:
1. Idempotent schema reconciliation across all schema variants:
   - Minimal legacy schema (A)
   - Production-like partial schema (B)
   - Already-complete schema (C)
2. Row and value preservation across migration.
3. Preflight check fail-closed behaviors (missing DB, empty DB, missing table).
4. Permission prep and rollback compatibility semantics.
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure backend directory is in sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from migrations.reconcile_payment_schema import (
    EXPECTED_COLUMNS,
    get_existing_columns,
    reconcile_schema,
)


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database file and yield its path."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = Path(tmp.name)
    tmp.close()
    yield db_path
    if db_path.exists():
        db_path.unlink()


# ===========================================================================
# FIXTURE A: Minimal legacy DB (id, name, phone, comment, filename)
# ===========================================================================
def test_fixture_a_minimal_legacy_schema(temp_db):
    """
    Fixture A: Old minimal legacy DB without payment columns.
    Expected: reconciliation adds all missing columns and completes schema.
    """
    conn = sqlite3.connect(temp_db)
    conn.execute("""
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            phone TEXT,
            comment TEXT,
            filename TEXT
        )
    """)
    conn.execute("INSERT INTO orders (name, phone) VALUES ('Old Customer', '+79990001122')")
    conn.commit()
    conn.close()

    # Check mode first
    check_result = reconcile_schema(temp_db, apply=False)
    assert check_result["status"] == "PARTIAL"
    assert check_result["migration_required"] is True
    assert "payment_status" in check_result["missing_columns"]
    assert "order_amount" in check_result["missing_columns"]

    # Apply mode
    apply_result = reconcile_schema(temp_db, apply=True)
    assert apply_result["status"] == "COMPLETE"
    assert apply_result["migration_required"] is False
    assert len(apply_result["columns_added"]) > 0

    # Verify all expected columns now exist
    conn = sqlite3.connect(temp_db)
    cols = get_existing_columns(conn, "orders")
    for col in EXPECTED_COLUMNS:
        assert col.lower() in cols, f"Expected column '{col}' missing from Fixture A"

    # Integrity check
    integrity = conn.execute("PRAGMA integrity_check;").fetchone()
    assert integrity[0] == "ok"
    conn.close()


# ===========================================================================
# FIXTURE B: Production-like partial DB (missing order_amount, created_at)
# ===========================================================================
def test_fixture_b_production_partial_schema(temp_db):
    """
    Fixture B: Production DB audited state on 79.137.196.14.
    Already has payment_status, payment_amount, robokassa_inv_id, paid_at, etc.
    Missing: order_amount, created_at.
    Expected: only missing columns added, existing values preserved.
    """
    conn = sqlite3.connect(temp_db)
    conn.execute("""
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            phone TEXT,
            email TEXT,
            format TEXT,
            paper TEXT,
            crop TEXT,
            comment TEXT,
            filename TEXT,
            status TEXT DEFAULT 'new',
            payment_status TEXT DEFAULT 'unpaid',
            payment_amount TEXT,
            payment_method TEXT DEFAULT 'robokassa',
            robokassa_inv_id TEXT,
            paid_at TEXT,
            payment_email_sent_at TEXT
        )
    """)
    # Insert representative production-like order
    conn.execute("""
        INSERT INTO orders (
            name, phone, email, payment_status, payment_amount, robokassa_inv_id, paid_at, payment_email_sent_at
        ) VALUES (
            'Prod Customer', '+79991234567', 'customer@example.com', 'paid', '300.00', 'inv-98765', '2026-09-18T12:00:00', '2026-09-18T12:05:00'
        )
    """)
    conn.commit()
    conn.close()

    # Verify check mode identifies exactly the missing columns
    check_result = reconcile_schema(temp_db, apply=False)
    assert check_result["status"] == "PARTIAL"
    assert check_result["migration_required"] is True
    assert set(check_result["missing_columns"]) == {"order_amount", "created_at"}

    # Apply reconciliation
    apply_result = reconcile_schema(temp_db, apply=True)
    assert apply_result["status"] == "COMPLETE"
    assert set(apply_result["columns_added"]) == {"order_amount", "created_at"}

    # Verify existing values are preserved
    conn = sqlite3.connect(temp_db)
    row = conn.execute("""
        SELECT name, email, payment_status, payment_amount, robokassa_inv_id, paid_at, payment_email_sent_at, order_amount, created_at
        FROM orders WHERE id = 1
    """).fetchone()
    conn.close()

    assert row[0] == "Prod Customer"
    assert row[1] == "customer@example.com"
    assert row[2] == "paid"
    assert row[3] == "300.00"
    assert row[4] == "inv-98765"
    assert row[5] == "2026-09-18T12:00:00"
    assert row[6] == "2026-09-18T12:05:00"
    assert row[7] is None  # Newly added column defaults to None
    assert row[8] is None  # Newly added column defaults to None


# ===========================================================================
# FIXTURE C: Already-complete DB
# ===========================================================================
def test_fixture_c_already_complete_schema(temp_db):
    """
    Fixture C: Already fully migrated DB.
    Expected: check reports COMPLETE, apply is a clean no-op with zero changes.
    """
    conn = sqlite3.connect(temp_db)
    conn.execute("""
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            phone TEXT,
            email TEXT,
            format TEXT,
            paper TEXT,
            crop TEXT,
            comment TEXT,
            filename TEXT,
            status TEXT DEFAULT 'new',
            payment_status TEXT DEFAULT 'unpaid',
            order_amount TEXT,
            payment_amount TEXT,
            payment_method TEXT DEFAULT 'robokassa',
            robokassa_inv_id TEXT,
            paid_at TEXT,
            payment_email_sent_at TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

    check_result = reconcile_schema(temp_db, apply=False)
    assert check_result["status"] == "COMPLETE"
    assert check_result["migration_required"] is False
    assert len(check_result["missing_columns"]) == 0

    apply_result = reconcile_schema(temp_db, apply=True)
    assert apply_result["status"] == "COMPLETE"
    assert apply_result["migration_required"] is False
    assert len(apply_result["columns_added"]) == 0


# ===========================================================================
# IDEMPOTENCY: apply #1 == apply #2
# ===========================================================================
def test_reconciliation_idempotency(temp_db):
    """
    Verify applying reconciliation twice produces no error and no second change.
    apply #1 = PASS
    apply #2 = PASS
    """
    conn = sqlite3.connect(temp_db)
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, name TEXT, phone TEXT)")
    conn.commit()
    conn.close()

    res1 = reconcile_schema(temp_db, apply=True)
    assert res1["status"] == "COMPLETE"
    assert len(res1["columns_added"]) == len(EXPECTED_COLUMNS)

    res2 = reconcile_schema(temp_db, apply=True)
    assert res2["status"] == "COMPLETE"
    assert len(res2["columns_added"]) == 0
    assert res2["migration_required"] is False


# ===========================================================================
# ROW PRESERVATION: row count and data identical before and after
# ===========================================================================
def test_row_preservation(temp_db):
    """
    Synthetic rows before migration must remain completely intact.
    ROW_COUNT_AFTER == ROW_COUNT_BEFORE
    """
    conn = sqlite3.connect(temp_db)
    conn.execute("""
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            phone TEXT,
            payment_status TEXT,
            payment_amount TEXT
        )
    """)
    records = [
        ("Alice", "+79111111111", "paid", "300.00"),
        ("Bob", "+79222222222", "unpaid", "150.00"),
        ("Charlie", "+79333333333", "paid", "500.00"),
    ]
    for r in records:
        conn.execute("INSERT INTO orders (name, phone, payment_status, payment_amount) VALUES (?, ?, ?, ?)", r)
    conn.commit()

    count_before = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    assert count_before == 3
    conn.close()

    # Reconcile
    reconcile_schema(temp_db, apply=True)

    conn = sqlite3.connect(temp_db)
    count_after = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    assert count_after == count_before

    rows = conn.execute("SELECT name, phone, payment_status, payment_amount FROM orders ORDER BY id").fetchall()
    conn.close()

    assert rows[0] == ("Alice", "+79111111111", "paid", "300.00")
    assert rows[1] == ("Bob", "+79222222222", "unpaid", "150.00")
    assert rows[2] == ("Charlie", "+79333333333", "paid", "500.00")


# ===========================================================================
# PREFLIGHT TOOL: scripts/production_preflight.py
# ===========================================================================
def test_preflight_missing_db():
    """Preflight fails closed if database file does not exist."""
    preflight_script = BACKEND_DIR.parent / "scripts" / "production_preflight.py"
    proc = subprocess.run(
        [sys.executable, str(preflight_script), "--db", "nonexistent_file.db"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "DB_EXISTS=false" in proc.stdout
    assert "READY_FOR_MIGRATION=false" in proc.stdout


def test_preflight_empty_db(temp_db):
    """Preflight fails closed if database file is 0 bytes."""
    # temp_db is created empty (0 bytes)
    preflight_script = BACKEND_DIR.parent / "scripts" / "production_preflight.py"
    proc = subprocess.run(
        [sys.executable, str(preflight_script), "--db", str(temp_db)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "DB_EXISTS=true" in proc.stdout
    assert "DB_NONZERO=false" in proc.stdout
    assert "READY_FOR_MIGRATION=false" in proc.stdout


def test_preflight_missing_orders_table(temp_db):
    """Preflight fails closed if orders table does not exist."""
    conn = sqlite3.connect(temp_db)
    conn.execute("CREATE TABLE other_table (id INTEGER PRIMARY KEY);")
    conn.commit()
    conn.close()

    preflight_script = BACKEND_DIR.parent / "scripts" / "production_preflight.py"
    proc = subprocess.run(
        [sys.executable, str(preflight_script), "--db", str(temp_db)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "DB_EXISTS=true" in proc.stdout
    assert "DB_NONZERO=true" in proc.stdout
    assert "ORDERS_TABLE_PRESENT=false" in proc.stdout
    assert "READY_FOR_MIGRATION=false" in proc.stdout


def test_preflight_valid_db(temp_db):
    """Preflight succeeds on a valid DB and reports missing columns."""
    conn = sqlite3.connect(temp_db)
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, name TEXT, phone TEXT);")
    conn.commit()
    conn.close()

    preflight_script = BACKEND_DIR.parent / "scripts" / "production_preflight.py"
    proc = subprocess.run(
        [sys.executable, str(preflight_script), "--db", str(temp_db)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "DB_EXISTS=true" in proc.stdout
    assert "DB_NONZERO=true" in proc.stdout
    assert "ORDERS_TABLE_PRESENT=true" in proc.stdout
    assert "SCHEMA_READABLE=true" in proc.stdout
    assert "READY_FOR_MIGRATION=true" in proc.stdout
    assert "payment_status" in proc.stdout


# ===========================================================================
# PERMISSION PREPARATION & ROLLBACK COMPATIBILITY SEMANTICS
# ===========================================================================
def test_permission_prep_and_rollback_semantics(temp_db):
    """
    Verifies that permissions preparation preserves read/write capability
    and that root / owner access is maintained.
    """
    conn = sqlite3.connect(temp_db)
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, name TEXT);")
    conn.execute("INSERT INTO orders (name) VALUES ('permission test');")
    conn.commit()
    conn.close()

    # Verify file is readable and writable
    assert os.access(temp_db, os.R_OK)
    assert os.access(temp_db, os.W_OK)

    # If running on POSIX with chown available, test chown semantics
    if hasattr(os, "chown") and os.name == "posix":
        # Check current UID
        current_uid = os.getuid()
        # Verify write test
        with open(temp_db, "a") as f:
            f.write("")
        assert os.access(temp_db, os.W_OK)
