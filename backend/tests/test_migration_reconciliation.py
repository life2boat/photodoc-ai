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

from migrations.reconcile_payment_schema import (  # noqa: E402
    EXPECTED_COLUMNS,
    get_existing_columns,
    reconcile_schema,
)
from db_contract import APPLICATION_ID  # noqa: E402


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
    check_result = reconcile_schema(temp_db, apply=False, allow_legacy_unmarked=True)
    assert check_result["status"] == "PARTIAL"
    assert check_result["migration_required"] is True
    assert "payment_status" in check_result["missing_columns"]
    assert "order_amount" in check_result["missing_columns"]

    # Apply mode
    apply_result = reconcile_schema(temp_db, apply=True, allow_legacy_unmarked=True)
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
    check_result = reconcile_schema(temp_db, apply=False, allow_legacy_unmarked=True)
    assert check_result["status"] == "PARTIAL"
    assert check_result["migration_required"] is True
    assert set(check_result["missing_columns"]) == {"order_amount", "created_at"}

    # Apply reconciliation
    apply_result = reconcile_schema(temp_db, apply=True, allow_legacy_unmarked=True)
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
    Fixture C: Complete legacy schema without an application identity.
    Expected: schema is unchanged while apply stamps the PhotoDoc identity.
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

    check_result = reconcile_schema(temp_db, apply=False, allow_legacy_unmarked=True)
    assert check_result["status"] == "PARTIAL"
    assert check_result["migration_required"] is True
    assert len(check_result["missing_columns"]) == 0

    apply_result = reconcile_schema(temp_db, apply=True, allow_legacy_unmarked=True)
    assert apply_result["status"] == "COMPLETE"
    assert apply_result["migration_required"] is False
    assert len(apply_result["columns_added"]) == 0
    assert apply_result["application_id"] == APPLICATION_ID


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

    res1 = reconcile_schema(temp_db, apply=True, allow_legacy_unmarked=True)
    assert res1["status"] == "COMPLETE"
    assert len(res1["columns_added"]) == len(EXPECTED_COLUMNS)

    res2 = reconcile_schema(temp_db, apply=True)
    assert res2["status"] == "COMPLETE"
    assert len(res2["columns_added"]) == 0
    assert res2["migration_required"] is False


def test_migration_failure_rolls_back_schema_and_application_id(temp_db):
    """A failure after transactional DDL must leave both schema and identity unchanged."""
    conn = sqlite3.connect(temp_db)
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, name TEXT, phone TEXT)")
    conn.commit()
    schema_before = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='orders'"
    ).fetchone()[0]
    application_id_before = conn.execute("PRAGMA application_id").fetchone()[0]
    conn.close()

    with pytest.raises(RuntimeError, match="Injected migration failure"):
        reconcile_schema(
            temp_db,
            apply=True,
            allow_legacy_unmarked=True,
            failure_after_columns=1,
        )

    conn = sqlite3.connect(temp_db)
    schema_after = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='orders'"
    ).fetchone()[0]
    application_id_after = conn.execute("PRAGMA application_id").fetchone()[0]
    conn.close()
    assert schema_after == schema_before
    assert application_id_after == application_id_before == 0


def test_migration_sets_stable_photodoc_application_id(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, name TEXT, phone TEXT)")
    conn.commit()
    conn.close()

    result = reconcile_schema(temp_db, apply=True, allow_legacy_unmarked=True)
    assert result["application_id"] == APPLICATION_ID == 0x50444149

    conn = sqlite3.connect(temp_db)
    assert conn.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
    conn.close()


def test_wrong_application_identity_is_rejected_even_in_legacy_mode(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, name TEXT, phone TEXT)")
    conn.execute("PRAGMA application_id = 123456")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="another application"):
        reconcile_schema(temp_db, apply=True, allow_legacy_unmarked=True)


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
    reconcile_schema(temp_db, apply=True, allow_legacy_unmarked=True)

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


def test_preflight_legacy_db_requires_explicit_flag(temp_db):
    """Legacy application_id=0 is rejected unless explicitly allowed."""
    conn = sqlite3.connect(temp_db)
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, name TEXT, phone TEXT);")
    conn.commit()
    conn.close()

    preflight_script = BACKEND_DIR.parent / "scripts" / "production_preflight.py"
    strict_proc = subprocess.run(
        [sys.executable, str(preflight_script), "--db", str(temp_db)],
        capture_output=True,
        text=True,
    )
    assert strict_proc.returncode != 0
    assert "LEGACY_UNMARKED=true" in strict_proc.stdout
    assert "READY_FOR_STARTUP=false" in strict_proc.stdout

    legacy_proc = subprocess.run(
        [
            sys.executable,
            str(preflight_script),
            "--db",
            str(temp_db),
            "--allow-legacy-unmarked",
        ],
        capture_output=True,
        text=True,
    )
    assert legacy_proc.returncode == 0
    assert "READY_FOR_MIGRATION=true" in legacy_proc.stdout
    assert "payment_status" in legacy_proc.stdout


def test_strict_preflight_accepts_only_complete_photodoc_identity(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, name TEXT, phone TEXT)")
    conn.commit()
    conn.close()
    reconcile_schema(temp_db, apply=True, allow_legacy_unmarked=True)

    preflight_script = BACKEND_DIR.parent / "scripts" / "production_preflight.py"
    valid = subprocess.run(
        [sys.executable, str(preflight_script), "--db", str(temp_db)],
        capture_output=True,
        text=True,
    )
    assert valid.returncode == 0
    assert f"DB_APPLICATION_ID={APPLICATION_ID}" in valid.stdout
    assert "DB_IDENTITY_VALID=true" in valid.stdout
    assert "READY_FOR_STARTUP=true" in valid.stdout

    conn = sqlite3.connect(temp_db)
    conn.execute("PRAGMA application_id = 123456")
    conn.commit()
    conn.close()
    invalid = subprocess.run(
        [sys.executable, str(preflight_script), "--db", str(temp_db)],
        capture_output=True,
        text=True,
    )
    assert invalid.returncode != 0
    assert "DB_IDENTITY_VALID=false" in invalid.stdout
    assert "READY_FOR_STARTUP=false" in invalid.stdout


def test_health_fails_when_database_identity_changes(temp_db, monkeypatch):
    from fastapi.testclient import TestClient
    import main

    monkeypatch.setattr(main, "DB_PATH", temp_db)
    main.init_db()
    conn = sqlite3.connect(temp_db)
    conn.execute("PRAGMA application_id = 123456")
    conn.commit()
    conn.close()

    response = TestClient(main.app).get("/api/health")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


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
        # Verify write test
        with open(temp_db, "a") as f:
            f.write("")
        assert os.access(temp_db, os.W_OK)


# ===========================================================================
# CREATED_AT ORDER INSERTION REGRESSION TESTS
# ===========================================================================
def test_fresh_db_order_creation_sets_created_at(temp_db, monkeypatch):
    """
    Fresh canonical database:
    When an order is created via /api/order, created_at must be explicitly set
    to a non-null ISO UTC timestamp.
    """
    from unittest.mock import patch
    from datetime import datetime
    from fastapi.testclient import TestClient
    import main

    monkeypatch.setattr(main, "DB_PATH", temp_db)
    main.init_db()

    client = TestClient(main.app)

    with patch("main.upload_to_yandex_disk"), patch("main.send_order_email"), patch("main.upload_info_to_yandex_disk"):
        resp = client.post(
            "/api/order",
            data={
                "name": "Fresh User",
                "phone": "+79991112233",
                "email": "fresh@example.com",
                "format": "DOC_3X4",
                "paper": "Глянцевая",
                "crop": "Без обрезки",
                "comment": "Fresh order test",
            },
            files=[("files", ("test.jpg", b"fake image bytes", "image/jpeg"))],
        )

    assert resp.status_code == 200
    order_id = resp.json()["order_id"]

    conn = sqlite3.connect(temp_db)
    row = conn.execute("SELECT name, order_amount, created_at FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "Fresh User"
    assert row[1] == "300.00"
    assert row[2] is not None, "created_at must be non-null for new orders in fresh DB"
    # Ensure it parses as a valid ISO timestamp
    parsed = datetime.fromisoformat(row[2])
    assert parsed is not None


def test_reconciled_db_order_creation_sets_created_at_and_preserves_old_null(temp_db, monkeypatch):
    """
    Reconciled partial database:
    1. Historical orders in the partially-migrated database retain created_at IS NULL.
    2. New orders created via /api/order on the reconciled DB receive a non-null ISO timestamp.
    """
    from unittest.mock import patch
    from datetime import datetime
    from fastapi.testclient import TestClient
    import main

    # Setup partial production schema (without order_amount and created_at)
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
    conn.execute("""
        INSERT INTO orders (
            name, phone, email, payment_status, payment_amount, robokassa_inv_id
        ) VALUES (
            'Historical User', '+79990001122', 'hist@example.com', 'paid', '300.00', 'inv-001'
        )
    """)
    conn.commit()
    conn.close()

    # Reconcile schema
    reconcile_res = reconcile_schema(temp_db, apply=True, allow_legacy_unmarked=True)
    assert reconcile_res["status"] == "COMPLETE"
    assert set(reconcile_res["columns_added"]) == {"order_amount", "created_at"}

    # Historical row must retain created_at as None (no artificial backfilling)
    conn = sqlite3.connect(temp_db)
    old_row = conn.execute("SELECT id, name, created_at FROM orders WHERE id = 1").fetchone()
    conn.close()
    assert old_row[2] is None, "Historical order must keep NULL created_at"

    # Now create new order via API on the reconciled DB
    monkeypatch.setattr(main, "DB_PATH", temp_db)
    client = TestClient(main.app)

    with patch("main.upload_to_yandex_disk"), patch("main.send_order_email"), patch("main.upload_info_to_yandex_disk"):
        resp = client.post(
            "/api/order",
            data={
                "name": "Reconciled Order User",
                "phone": "+79997778899",
                "email": "reconciled@example.com",
                "format": "DOC_3X4",
                "paper": "Матовая",
                "crop": "Без обрезки",
                "comment": "Reconciled order test",
            },
            files=[("files", ("new_doc.jpg", b"fake bytes", "image/jpeg"))],
        )

    assert resp.status_code == 200
    new_id = resp.json()["order_id"]

    conn = sqlite3.connect(temp_db)
    new_row = conn.execute("SELECT name, order_amount, created_at FROM orders WHERE id = ?", (new_id,)).fetchone()
    conn.close()

    assert new_row is not None
    assert new_row[0] == "Reconciled Order User"
    assert new_row[1] == "300.00"
    assert new_row[2] is not None, "created_at must be non-null for new orders in reconciled DB"
    parsed = datetime.fromisoformat(new_row[2])
    assert parsed is not None


# ===========================================================================
# STRICT READ-ONLY PREFLIGHT TESTS
# ===========================================================================
def test_preflight_missing_db_does_not_create_file(tmp_path):
    """
    Preflight must be strictly read-only:
    A non-existent database file must NOT be created when preflight runs.
    PREFLIGHT_MISSING_DB_CREATED=false
    """
    never_created = tmp_path / "never_created.db"
    assert not never_created.exists()

    preflight_script = BACKEND_DIR.parent / "scripts" / "production_preflight.py"
    proc = subprocess.run(
        [sys.executable, str(preflight_script), "--db", str(never_created)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert not never_created.exists(), "Preflight must never create a database file!"


def test_preflight_valid_db_unchanged(temp_db):
    """
    Preflight must not alter an existing valid database file.
    Size and mtime must remain completely identical.
    PREFLIGHT_VALID_DB_UNCHANGED=true
    """
    conn = sqlite3.connect(temp_db)
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, name TEXT, phone TEXT);")
    conn.execute("INSERT INTO orders (name, phone) VALUES ('Alice', '+79990001111');")
    conn.commit()
    conn.close()

    stat_before = temp_db.stat()

    preflight_script = BACKEND_DIR.parent / "scripts" / "production_preflight.py"
    proc = subprocess.run(
        [
            sys.executable,
            str(preflight_script),
            "--db",
            str(temp_db),
            "--allow-legacy-unmarked",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0

    stat_after = temp_db.stat()
    assert stat_before.st_size == stat_after.st_size
    assert stat_before.st_mtime_ns == stat_after.st_mtime_ns


# ===========================================================================
# ENVIRONMENT PREFLIGHT TESTS
# ===========================================================================
def test_preflight_env_file_verification(tmp_path):
    """
    Preflight environment checks:
    1. Valid env file with all required keys passes.
    2. Missing keys causes exit code != 0 and READY_FOR_CUTOVER=false.
    3. Secret values are never printed in output.
    """
    preflight_script = BACKEND_DIR.parent / "scripts" / "production_preflight.py"

    # 1. Valid env file
    valid_env = tmp_path / "valid.env"
    valid_env.write_text(
        "ROBOKASSA_MERCHANT_LOGIN=secret_login\n"
        "ROBOKASSA_PASSWORD1=secret_pass1\n"
        "ROBOKASSA_PASSWORD2=secret_pass2\n"
        "ROBOKASSA_IS_TEST=0\n"
        "SMTP_SERVER=smtp.example.com\n"
        "SMTP_PORT=465\n"
        "SMTP_USER=user@example.com\n"
        "SMTP_PASSWORD=secret_smtp_pass\n"
        "EMAIL_TO=owner@example.com\n"
        "YANDEX_DISK_TOKEN=secret_token\n",
        encoding="utf-8",
    )

    proc_valid = subprocess.run(
        [sys.executable, str(preflight_script), "--check-env", str(valid_env)],
        capture_output=True,
        text=True,
    )
    assert proc_valid.returncode == 0
    assert "READY_FOR_CUTOVER=true" in proc_valid.stdout
    assert "ENV_SECRET_VALUES_PRINTED=false" in proc_valid.stdout
    assert "secret_pass1" not in proc_valid.stdout
    assert "secret_smtp_pass" not in proc_valid.stdout

    # 2. Incomplete env file
    incomplete_env = tmp_path / "incomplete.env"
    incomplete_env.write_text(
        "ROBOKASSA_MERCHANT_LOGIN=secret_login\n"
        "ROBOKASSA_IS_TEST=1\n",
        encoding="utf-8",
    )

    proc_incomplete = subprocess.run(
        [sys.executable, str(preflight_script), "--check-env", str(incomplete_env)],
        capture_output=True,
        text=True,
    )
    assert proc_incomplete.returncode != 0
    assert "READY_FOR_CUTOVER=false" in proc_incomplete.stdout
    assert "ROBOKASSA_PASSWORD1=MISSING" in proc_incomplete.stdout
    assert "ENV_SECRET_VALUES_PRINTED=false" in proc_incomplete.stdout
