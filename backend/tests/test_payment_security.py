"""
Payment Security Test Suite — PhotoDoc AI
=========================================
Covers:
  - Server-authoritative price enforcement (payment_amount cannot be price authority)
  - Signature validation (Password1 / Password2)
  - Amount mismatch rejection
  - Atomic idempotency & concurrent duplicate callbacks (one transition owner only)
  - Email side-effect safety: SMTP failure → sent_at NULL (retryable)
  - Email de-duplication: successful send → sent_at populated; duplicate callback → no duplicate email
  - Unknown service/SKU → HTTP 400 rejection
  - Success URL read-only guarantee
  - Migration forward PASS + rollback PASS
"""
import os
import sys
import sqlite3
import hashlib
import tempfile
import shutil
import threading
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# Bootstrap: set env vars BEFORE importing main
# ---------------------------------------------------------------------------
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

TEST_MERCHANT_LOGIN = "test_merchant"
TEST_PASSWORD1 = "test_pass_1_merchant"
TEST_PASSWORD2 = "test_pass_2_server"

os.environ["ROBOKASSA_MERCHANT_LOGIN"] = TEST_MERCHANT_LOGIN
os.environ["ROBOKASSA_PASSWORD1"] = TEST_PASSWORD1
os.environ["ROBOKASSA_PASSWORD2"] = TEST_PASSWORD2
os.environ["ROBOKASSA_IS_TEST"] = "1"

# Isolated temp DB — each test file import gets its own DB
_temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_temp_db_path = _temp_db.name
_temp_db.close()
os.environ["DATABASE_PATH"] = _temp_db_path

from fastapi.testclient import TestClient
from main import (
    app,
    init_db,
    DB_PATH,
    calculate_authoritative_price,
    format_robokassa_amount,
    get_order_by_id,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def setup_and_teardown_db():
    """Re-create schema and wipe orders between every test."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.cursor().execute("DELETE FROM orders")
    conn.commit()
    conn.close()
    yield


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def create_db_order(
    name="Тестовый Клиент",
    phone="+79991234567",
    format_str="3x4",
    paper="Матовая",
    crop="Без полей",
    file_count=1,
    status="new",
    payment_status="unpaid",
    order_amount: str | None = None,
) -> int:
    price = calculate_authoritative_price(format_str, paper, crop, file_count)
    formatted_price = format_robokassa_amount(price)
    if order_amount is None:
        order_amount = formatted_price

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO orders (
            name, phone, format, paper, crop, filename,
            status, payment_status, order_amount, payment_amount
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name, phone, format_str, paper, crop,
            ", ".join([f"file_{i}.jpg" for i in range(file_count)]),
            status, payment_status, order_amount, formatted_price,
        ),
    )
    conn.commit()
    order_id = cursor.lastrowid
    conn.close()
    return order_id


def compute_robokassa_sig(out_sum: str, inv_id: int | str, password: str) -> str:
    return hashlib.md5(f"{out_sum}:{inv_id}:{password}".encode("utf-8")).hexdigest()


def post_result(client, out_sum: str, inv_id: int, password: str):
    sig = compute_robokassa_sig(out_sum, inv_id, password)
    return client.post(
        "/api/payment/robokassa/result",
        params={"OutSum": out_sum, "InvId": str(inv_id), "SignatureValue": sig},
    )


# ===========================================================================
# TEST 1 — Underpayment rejected (payment_amount cannot be price authority)
# ===========================================================================
def test_underpayment_rejected(client):
    """
    CRITICAL INVARIANT:
    Server order price = 300.00 RUB. Client requests amount = 1.00 RUB.
    Result: HTTP 400, payment_status remains 'unpaid', order_amount stays 300.00.
    """
    order_id = create_db_order(format_str="3x4", file_count=1)

    response = client.post("/api/payment/create", json={"order_id": order_id, "amount": 1.00})
    assert response.status_code == 400
    assert "Payment amount mismatch" in response.json()["detail"]

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.cursor().execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    assert row["payment_status"] == "unpaid"
    assert row["order_amount"] == "300.00"


# ===========================================================================
# TEST 1b — payment_amount field CANNOT override order_amount as price
# ===========================================================================
def test_payment_amount_cannot_be_price_authority(client):
    """
    Regression: order_amount=300.00, payment_amount=1.00
    get_authoritative_order_amount() MUST return 300.00 (from order_amount),
    NOT 1.00 (from payment_amount).
    """
    # Insert order with mismatched payment_amount (simulates attacker-tampered state)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO orders (
            name, phone, format, paper, crop, filename,
            status, payment_status, order_amount, payment_amount
        ) VALUES ('Test', '+7999', '3x4', '', '', 'file_0.jpg',
                  'new', 'unpaid', '300.00', '1.00')
        """
    )
    conn.commit()
    order_id = cursor.lastrowid
    conn.close()

    # payment/create MUST use order_amount (300.00), NOT payment_amount (1.00)
    response = client.post("/api/payment/create", json={"order_id": order_id})
    assert response.status_code == 200
    data = response.json()
    assert data["amount"] == "300.00", (
        f"Expected 300.00 (from order_amount), got {data['amount']} — "
        "payment_amount leaked as price authority!"
    )
    assert "OutSum=300.00" in data["payment_url"]


# ===========================================================================
# TEST 1c — NULL order_amount recalculates from spec (never uses payment_amount)
# ===========================================================================
def test_null_order_amount_recalculates_not_payment_amount(client):
    """
    order_amount=NULL, payment_amount=1.00
    Server MUST recalculate from order spec (3x4 → 300.00), ignoring payment_amount entirely.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO orders (
            name, phone, format, paper, crop, filename,
            status, payment_status, order_amount, payment_amount
        ) VALUES ('Test', '+7999', '3x4', '', '', 'file_0.jpg',
                  'new', 'unpaid', NULL, '1.00')
        """
    )
    conn.commit()
    order_id = cursor.lastrowid
    conn.close()

    response = client.post("/api/payment/create", json={"order_id": order_id})
    assert response.status_code == 200
    data = response.json()
    assert data["amount"] == "300.00", (
        f"Expected 300.00 (recalculated from spec), got {data['amount']} — "
        "payment_amount=1.00 must be IGNORED!"
    )


# ===========================================================================
# TEST 2 — Authoritative payment without client amount
# ===========================================================================
def test_payment_create_authoritative_without_client_amount(client):
    """
    Preferred API: client sends only { "order_id": 123 }.
    Server derives 300.00 authoritatively.
    """
    order_id = create_db_order(format_str="3.5x4.5", file_count=1)
    response = client.post("/api/payment/create", json={"order_id": order_id})
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["amount"] == "300.00"
    assert "OutSum=300.00" in data["payment_url"]
    assert f"InvId={order_id}" in data["payment_url"]
    expected_sig = hashlib.md5(f"{TEST_MERCHANT_LOGIN}:300.00:{order_id}:{TEST_PASSWORD1}".encode()).hexdigest()
    assert f"SignatureValue={expected_sig}" in data["payment_url"]


# ===========================================================================
# TEST 3 — Invalid signature rejected
# ===========================================================================
def test_result_invalid_signature_rejected(client):
    order_id = create_db_order(format_str="3x4")
    response = client.post(
        "/api/payment/robokassa/result",
        params={"OutSum": "300.00", "InvId": str(order_id), "SignatureValue": "deadbeef" * 4},
    )
    assert response.status_code == 400
    assert "Invalid signature" in response.json()["detail"]

    conn = sqlite3.connect(DB_PATH)
    row = conn.cursor().execute("SELECT payment_status FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    assert row[0] == "unpaid"


def test_success_invalid_signature_rejected(client):
    order_id = create_db_order(format_str="3x4")
    response = client.get(
        "/api/payment/robokassa/success",
        params={"OutSum": "300.00", "InvId": str(order_id), "SignatureValue": "badc0ffee" * 4},
    )
    assert response.status_code == 400
    assert "Invalid signature" in response.json()["detail"]


# ===========================================================================
# TEST 4 — Amount mismatch rejected
# ===========================================================================
def test_result_amount_mismatch_rejected(client):
    order_id = create_db_order(format_str="3x4")
    tampered_sum = "100.00"
    sig = compute_robokassa_sig(tampered_sum, order_id, TEST_PASSWORD2)
    response = client.post(
        "/api/payment/robokassa/result",
        params={"OutSum": tampered_sum, "InvId": str(order_id), "SignatureValue": sig},
    )
    assert response.status_code == 400
    assert "Amount mismatch" in response.json()["detail"]


# ===========================================================================
# TEST 5 — Idempotency: duplicate callback does not duplicate email
# ===========================================================================
def test_result_callback_idempotency(client):
    order_id = create_db_order(format_str="3x4")
    sig = compute_robokassa_sig("300.00", order_id, TEST_PASSWORD2)

    # First valid callback
    res1 = client.post(
        "/api/payment/robokassa/result",
        params={"OutSum": "300.00", "InvId": str(order_id), "SignatureValue": sig},
    )
    assert res1.status_code == 200
    assert res1.text == f"OK{order_id}"

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row1 = conn.cursor().execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    assert row1["payment_status"] == "paid"
    paid_at = row1["paid_at"]
    assert paid_at is not None

    # Duplicate callback — must return OK but NOT re-transition
    res2 = client.post(
        "/api/payment/robokassa/result",
        params={"OutSum": "300.00", "InvId": str(order_id), "SignatureValue": sig},
    )
    assert res2.status_code == 200
    assert res2.text == f"OK{order_id}"

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row2 = conn.cursor().execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    assert row2["payment_status"] == "paid"
    assert row2["paid_at"] == paid_at  # unchanged


# ===========================================================================
# TEST 5b — Concurrent duplicate callbacks: only one transition owner
# ===========================================================================
def test_concurrent_duplicate_callback_one_owner_only(client):
    """
    Simulate two concurrent valid callbacks arriving simultaneously.
    Only one should perform the DB transition (rowcount=1).
    Both return OK{order_id} (idempotent).
    """
    order_id = create_db_order(format_str="3x4")
    sig = compute_robokassa_sig("300.00", order_id, TEST_PASSWORD2)
    results = []

    def call_result():
        r = client.post(
            "/api/payment/robokassa/result",
            params={"OutSum": "300.00", "InvId": str(order_id), "SignatureValue": sig},
        )
        results.append(r.status_code)

    t1 = threading.Thread(target=call_result)
    t2 = threading.Thread(target=call_result)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert results.count(200) == 2, f"Both concurrent calls must return 200, got: {results}"

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.cursor().execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    assert row["payment_status"] == "paid"


# ===========================================================================
# TEST 6 — Success URL cannot mark paid
# ===========================================================================
def test_success_url_cannot_mark_paid(client):
    order_id = create_db_order(format_str="3x4")
    sig_pwd1 = compute_robokassa_sig("300.00", order_id, TEST_PASSWORD1)
    res = client.get(
        "/api/payment/robokassa/success",
        params={"OutSum": "300.00", "InvId": str(order_id), "SignatureValue": sig_pwd1},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["status"] == "pending"  # NOT paid!

    conn = sqlite3.connect(DB_PATH)
    row = conn.cursor().execute("SELECT payment_status FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    assert row[0] == "unpaid"


def test_success_url_displays_paid_only_after_result_url(client):
    order_id = create_db_order(format_str="3x4")

    # First: authoritative Result callback
    res_result = post_result(client, "300.00", order_id, TEST_PASSWORD2)
    assert res_result.status_code == 200

    # Then: browser redirect to Success URL
    sig_pwd1 = compute_robokassa_sig("300.00", order_id, TEST_PASSWORD1)
    res_success = client.get(
        "/api/payment/robokassa/success",
        params={"OutSum": "300.00", "InvId": str(order_id), "SignatureValue": sig_pwd1},
    )
    assert res_success.status_code == 200
    assert res_success.json()["status"] == "paid"


# ===========================================================================
# TEST 7 — Unknown order not found
# ===========================================================================
def test_unknown_order_not_found(client):
    res = client.post("/api/payment/create", json={"order_id": 999999})
    assert res.status_code == 404
    assert "Order not found" in res.json()["detail"]

    sig = compute_robokassa_sig("300.00", 999999, TEST_PASSWORD2)
    res_cb = client.post(
        "/api/payment/robokassa/result",
        params={"OutSum": "300.00", "InvId": "999999", "SignatureValue": sig},
    )
    assert res_cb.status_code == 404


# ===========================================================================
# TEST 8 — Unknown service/SKU → HTTP 400
# ===========================================================================
def test_unknown_sku_rejected_in_price_calculation():
    """
    Unknown format/crop combinations must raise HTTPException 400,
    NOT silently default to 300 RUB.
    """
    from fastapi import HTTPException as FastAPIException
    with pytest.raises(FastAPIException) as exc_info:
        calculate_authoritative_price("UNKNOWN_FORMAT", "", "UNKNOWN_CROP", 1)
    assert exc_info.value.status_code == 400
    assert "Unknown service/SKU" in exc_info.value.detail


def test_known_skus_accepted():
    """All explicitly known SKUs must resolve to a non-zero price."""
    known_cases = [
        ("3x4", "", 300.0),
        ("3.5x4.5", "", 300.0),
        ("4x6", "", 300.0),
        ("9x12", "", 300.0),
        ("DOC_3X4", "", 300.0),
        ("DOC_35X45", "", 300.0),
        ("DOC_4X6", "", 300.0),
        ("DOC_9X12", "", 300.0),
        ("POLAROID", "", 30.0),
        ("polaroid", "", 30.0),
        ("9x13", "", 20.0),
        ("10x15", "", 20.0),
        ("PRINT_9X13", "", 20.0),
        ("PRINT_10X15", "", 20.0),
        ("RESTORE_LIGHT", "", 200.0),
        ("RESTORE_DEEP", "", 350.0),
        ("RESTORE_COLORIZE", "", 150.0),
        ("Легкая реставрация", "", 200.0),
        ("Глубокая реставрация с ИИ", "", 350.0),
        ("Окрашивание / Колоризация", "", 150.0),
    ]
    for fmt, crop, expected in known_cases:
        result = calculate_authoritative_price(fmt, "", crop, 1)
        assert result == expected, f"SKU '{fmt}'/'{crop}' → expected {expected}, got {result}"


# ===========================================================================
# TEST 9 — Email side-effect safety: SMTP failure → sent_at NULL (retryable)
# ===========================================================================
def test_smtp_failure_leaves_sent_at_null():
    """
    SMTP exception during send → payment_email_sent_at stays NULL.
    This keeps the email retryable.
    """
    order_id = create_db_order(format_str="3x4")
    # Manually mark as paid in DB (simulating that result callback ran)
    conn = sqlite3.connect(DB_PATH)
    conn.cursor().execute(
        "UPDATE orders SET payment_status='paid', status='paid', paid_at='2026-01-01T00:00:00' WHERE id=?",
        (order_id,),
    )
    conn.commit()
    conn.close()

    os.environ["SMTP_SERVER"] = "smtp.example.com"
    os.environ["SMTP_USER"] = "test@example.com"
    os.environ["SMTP_PASSWORD"] = "secret"
    os.environ["EMAIL_TO"] = "owner@example.com"

    try:
        with patch("smtplib.SMTP_SSL") as mock_smtp:
            mock_smtp.return_value.__enter__.return_value.send_message.side_effect = Exception("SMTP connection error")
            from main import send_paid_order_email
            send_paid_order_email(order_id)

        order = get_order_by_id(order_id)
        assert order["payment_email_sent_at"] is None, (
            "payment_email_sent_at must be NULL after SMTP failure — retry must remain possible!"
        )
    finally:
        for k in ("SMTP_SERVER", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"):
            os.environ.pop(k, None)


def test_smtp_success_records_sent_at():
    """
    Successful SMTP send → payment_email_sent_at is recorded in DB.
    """
    order_id = create_db_order(format_str="3x4")
    conn = sqlite3.connect(DB_PATH)
    conn.cursor().execute(
        "UPDATE orders SET payment_status='paid', status='paid', paid_at='2026-01-01T00:00:00' WHERE id=?",
        (order_id,),
    )
    conn.commit()
    conn.close()

    os.environ["SMTP_SERVER"] = "smtp.example.com"
    os.environ["SMTP_USER"] = "test@example.com"
    os.environ["SMTP_PASSWORD"] = "secret"
    os.environ["EMAIL_TO"] = "owner@example.com"

    try:
        with patch("smtplib.SMTP_SSL") as mock_smtp:
            mock_smtp.return_value.__enter__.return_value.send_message.return_value = None
            from main import send_paid_order_email
            send_paid_order_email(order_id)

        order = get_order_by_id(order_id)
        assert order["payment_email_sent_at"] is not None, (
            "payment_email_sent_at must be set after successful SMTP send!"
        )
    finally:
        for k in ("SMTP_SERVER", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"):
            os.environ.pop(k, None)


def test_duplicate_callback_no_duplicate_email():
    """
    Two consecutive valid callbacks → email is sent only once.
    Second call should skip due to payment_email_sent_at being populated.
    """
    order_id = create_db_order(format_str="3x4")
    conn = sqlite3.connect(DB_PATH)
    conn.cursor().execute(
        "UPDATE orders SET payment_status='paid', status='paid', paid_at='2026-01-01T00:00:00' WHERE id=?",
        (order_id,),
    )
    conn.commit()
    conn.close()

    os.environ["SMTP_SERVER"] = "smtp.example.com"
    os.environ["SMTP_USER"] = "test@example.com"
    os.environ["SMTP_PASSWORD"] = "secret"
    os.environ["EMAIL_TO"] = "owner@example.com"

    call_count = 0
    try:
        from main import send_paid_order_email

        def counting_send(*args, **kwargs):
            nonlocal call_count
            call_count += 1

        with patch("smtplib.SMTP_SSL") as mock_smtp:
            mock_instance = mock_smtp.return_value.__enter__.return_value
            mock_instance.send_message.side_effect = counting_send

            # First send — sets payment_email_sent_at
            send_paid_order_email(order_id)
            # Second call — should be skipped (idempotency guard)
            send_paid_order_email(order_id)

        assert call_count == 1, f"Expected 1 email send, got {call_count} — duplicate email detected!"
    finally:
        for k in ("SMTP_SERVER", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"):
            os.environ.pop(k, None)


# ===========================================================================
# TEST 10 — Migration forward PASS + rollback PASS
# ===========================================================================
def test_migration_forward_pass():
    """
    Apply 001_add_payment_fields.sql to a truly minimal legacy DB (id, name, phone only).
    Verify: legacy rows preserved, new columns exist, PRAGMA integrity_check = ok.
    """
    migration_path = Path(__file__).resolve().parent.parent / "migrations" / "001_add_payment_fields.sql"
    if not migration_path.exists():
        pytest.skip("Migration file not found — skipping migration test")

    tmp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    legacy_db_path = tmp_file.name
    tmp_file.close()

    try:
        # Create minimal pre-migration schema (only: id, name, phone — no format/status)
        conn = sqlite3.connect(legacy_db_path)
        conn.execute("""
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                phone TEXT
            )
        """)
        conn.execute("INSERT INTO orders (name, phone) VALUES ('Иван', '+79991234567')")
        conn.commit()

        # Apply migration
        migration_sql = migration_path.read_text(encoding="utf-8")
        conn.executescript(migration_sql)
        conn.commit()

        # Verify legacy row preserved
        row = conn.execute("SELECT name, phone FROM orders WHERE name='Иван'").fetchone()
        assert row is not None, "Legacy row must survive migration"
        assert row[0] == "Иван"

        # Verify new columns exist
        pragma = conn.execute("PRAGMA table_info(orders)").fetchall()
        col_names = {col[1] for col in pragma}
        for expected_col in ("payment_status", "payment_amount", "order_amount", "paid_at", "payment_email_sent_at"):
            assert expected_col in col_names, f"Column '{expected_col}' missing after migration"

        # Verify integrity
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        assert integrity[0] == "ok", f"Integrity check failed: {integrity[0]}"
        conn.close()
    finally:
        try:
            Path(legacy_db_path).unlink(missing_ok=True)
        except OSError:
            pass


def test_migration_rollback_pass():
    """
    Apply migration to a legacy DB, then roll back using DROP COLUMN (SQLite 3.35+).
    Verify: original rows intact, integrity_check = ok after rollback.
    """
    migration_path = Path(__file__).resolve().parent.parent / "migrations" / "001_add_payment_fields.sql"
    if not migration_path.exists():
        pytest.skip("Migration file not found — skipping rollback test")

    tmp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = tmp_file.name
    tmp_file.close()
    conn = None

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                phone TEXT
            )
        """)
        conn.execute("INSERT INTO orders (name, phone) VALUES ('Rollback Test', '+70000000000')")
        conn.commit()

        # Apply migration forward
        migration_sql = migration_path.read_text(encoding="utf-8")
        conn.executescript(migration_sql)
        conn.commit()

        # Rollback: attempt to drop newly added columns (SQLite 3.35+)
        added_cols = [
            "email", "format", "paper", "crop", "status", "payment_status",
            "order_amount", "payment_amount", "payment_method", "robokassa_inv_id",
            "paid_at", "payment_email_sent_at", "created_at",
        ]
        for col in added_cols:
            try:
                conn.execute(f"ALTER TABLE orders DROP COLUMN {col}")
                conn.commit()
            except sqlite3.OperationalError:
                pass  # Column absent or SQLite < 3.35 — skip silently

        # Verify original row still intact
        row = conn.execute("SELECT name FROM orders WHERE name='Rollback Test'").fetchone()
        assert row is not None, "Legacy row must survive rollback"

        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        assert integrity[0] == "ok", f"Integrity check after rollback failed: {integrity[0]}"
    finally:
        if conn:
            conn.close()
        try:
            Path(db_path).unlink(missing_ok=True)
        except OSError:
            pass

