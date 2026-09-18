import os
import sys
import sqlite3
import hashlib
import tempfile
import pytest
from pathlib import Path

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from fastapi.testclient import TestClient

# Mock credentials for offline testing (no real credentials or network calls)
TEST_MERCHANT_LOGIN = "test_merchant"
TEST_PASSWORD1 = "test_pass_1_merchant"
TEST_PASSWORD2 = "test_pass_2_server"

os.environ["ROBOKASSA_MERCHANT_LOGIN"] = TEST_MERCHANT_LOGIN
os.environ["ROBOKASSA_PASSWORD1"] = TEST_PASSWORD1
os.environ["ROBOKASSA_PASSWORD2"] = TEST_PASSWORD2
os.environ["ROBOKASSA_IS_TEST"] = "1"

# Create a temporary SQLite database for tests
temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
temp_db_path = temp_db.name
temp_db.close()
os.environ["DATABASE_PATH"] = temp_db_path

# Import app after setting env vars
from main import app, init_db, DB_PATH, calculate_authoritative_price, format_robokassa_amount


@pytest.fixture(autouse=True)
def setup_and_teardown_db():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM orders")
    conn.commit()
    conn.close()
    yield


@pytest.fixture
def client():
    return TestClient(app)


def create_db_order(
    name="Тестовый Клиент",
    phone="+79991234567",
    format_str="3x4",
    paper="Матовая",
    crop="Без полей",
    file_count=1,
    status="new",
    payment_status="unpaid"
) -> int:
    price = calculate_authoritative_price(format_str, paper, crop, file_count)
    formatted_price = format_robokassa_amount(price)

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
            status, payment_status, formatted_price, formatted_price
        )
    )
    conn.commit()
    order_id = cursor.lastrowid
    conn.close()
    return order_id


def compute_robokassa_sig(out_sum: str, inv_id: int | str, password: str) -> str:
    return hashlib.md5(f"{out_sum}:{inv_id}:{password}".encode("utf-8")).hexdigest()


# --- TEST 1: P0 UNDERPAYMENT TEST ---
def test_underpayment_rejected(client):
    """
    CRITICAL INVARIANT:
    Actual server-side order price = 300 RUB.
    Client requests amount = 1 RUB.
    Required result: PAYMENT_FOR_1_RUB_CREATED=false (HTTP 400 Bad Request).
    """
    order_id = create_db_order(format_str="3x4", file_count=1) # Price = 300.00 RUB

    # Malicious client sends amount: 1.00
    response = client.post("/api/payment/create", json={"order_id": order_id, "amount": 1.00})

    assert response.status_code == 400
    assert "Payment amount mismatch" in response.json()["detail"]

    # Verify payment URL for 1.00 was NEVER generated and order remains unpaid/unmodified
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.cursor().execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    assert row["payment_status"] == "unpaid"
    assert row["order_amount"] == "300.00"


def test_payment_create_authoritative_without_client_amount(client):
    """
    Preferred API contract: client sends only { "order_id": 123 }.
    Server derives 300.00 authoritatively from server order record.
    """
    order_id = create_db_order(format_str="3.5x4.5", file_count=1)

    response = client.post("/api/payment/create", json={"order_id": order_id})
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["amount"] == "300.00"
    assert "OutSum=300.00" in data["payment_url"]
    assert f"InvId={order_id}" in data["payment_url"]

    # Check signature is valid
    expected_sig = hashlib.md5(f"{TEST_MERCHANT_LOGIN}:300.00:{order_id}:{TEST_PASSWORD1}".encode("utf-8")).hexdigest()
    assert f"SignatureValue={expected_sig}" in data["payment_url"]


# --- TEST 2: INVALID SIGNATURE TEST ---
def test_result_invalid_signature_rejected(client):
    order_id = create_db_order(format_str="3x4")
    invalid_sig = "deadbeefdeadbeefdeadbeefdeadbeef"

    response = client.post(
        "/api/payment/robokassa/result",
        params={"OutSum": "300.00", "InvId": str(order_id), "SignatureValue": invalid_sig}
    )
    assert response.status_code == 400
    assert "Invalid signature" in response.json()["detail"]

    # Order must remain unpaid
    conn = sqlite3.connect(DB_PATH)
    row = conn.cursor().execute("SELECT payment_status FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    assert row[0] == "unpaid"


def test_success_invalid_signature_rejected(client):
    order_id = create_db_order(format_str="3x4")
    invalid_sig = "badc0ffeebadc0ffeebadc0ffee00000"

    response = client.get(
        "/api/payment/robokassa/success",
        params={"OutSum": "300.00", "InvId": str(order_id), "SignatureValue": invalid_sig}
    )
    assert response.status_code == 400
    assert "Invalid signature" in response.json()["detail"]


# --- TEST 3: AMOUNT MISMATCH TEST ---
def test_result_amount_mismatch_rejected(client):
    """
    Attacker gets a signed request for 100.00 on a different order or tampers OutSum
    even if signature matches the tampered OutSum with Password2.
    Server MUST verify OutSum == authoritative order_amount.
    """
    order_id = create_db_order(format_str="3x4") # Server price: 300.00
    tampered_sum = "100.00"
    valid_sig_for_tampered_sum = compute_robokassa_sig(tampered_sum, order_id, TEST_PASSWORD2)

    response = client.post(
        "/api/payment/robokassa/result",
        params={"OutSum": tampered_sum, "InvId": str(order_id), "SignatureValue": valid_sig_for_tampered_sum}
    )
    assert response.status_code == 400
    assert "Amount mismatch" in response.json()["detail"]


# --- TEST 4: IDEMPOTENCY & DUPLICATE CALLBACK TEST ---
def test_result_callback_idempotency(client):
    order_id = create_db_order(format_str="3x4") # 300.00
    valid_sig = compute_robokassa_sig("300.00", order_id, TEST_PASSWORD2)

    # 1. First valid callback
    res1 = client.post(
        "/api/payment/robokassa/result",
        params={"OutSum": "300.00", "InvId": str(order_id), "SignatureValue": valid_sig}
    )
    assert res1.status_code == 200
    assert res1.text == f"OK{order_id}"

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row1 = conn.cursor().execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    assert row1["payment_status"] == "paid"
    assert row1["status"] == "paid"
    paid_at = row1["paid_at"]
    assert paid_at is not None

    # 2. Duplicate valid callback (replay)
    res2 = client.post(
        "/api/payment/robokassa/result",
        params={"OutSum": "300.00", "InvId": str(order_id), "SignatureValue": valid_sig}
    )
    assert res2.status_code == 200
    assert res2.text == f"OK{order_id}"

    # Status and paid_at must remain intact
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row2 = conn.cursor().execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    assert row2["payment_status"] == "paid"
    assert row2["paid_at"] == paid_at


# --- TEST 5: SUCCESS URL CANNOT MARK PAID TEST ---
def test_success_url_cannot_mark_paid(client):
    """
    CRITICAL SECURITY TEST:
    Success URL is customer/browser return UX ONLY.
    Even with valid Password1 signature, Success URL MUST NOT change payment_status to 'paid'.
    """
    order_id = create_db_order(format_str="3x4") # Starts as unpaid
    sig_pwd1 = compute_robokassa_sig("300.00", order_id, TEST_PASSWORD1)

    # Customer redirected to Success URL before Result URL arrives
    res = client.get(
        "/api/payment/robokassa/success",
        params={"OutSum": "300.00", "InvId": str(order_id), "SignatureValue": sig_pwd1}
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["status"] == "pending" # NOT paid!

    # Verify database was NOT updated to paid
    conn = sqlite3.connect(DB_PATH)
    row = conn.cursor().execute("SELECT payment_status FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    assert row[0] == "unpaid"


def test_success_url_displays_paid_only_after_result_url(client):
    """
    If Result URL already processed payment, Success URL reports status: 'paid'.
    """
    order_id = create_db_order(format_str="3x4")
    sig_pwd2 = compute_robokassa_sig("300.00", order_id, TEST_PASSWORD2)
    sig_pwd1 = compute_robokassa_sig("300.00", order_id, TEST_PASSWORD1)

    # 1. Authoritative Result callback arrives
    res_result = client.post(
        "/api/payment/robokassa/result",
        params={"OutSum": "300.00", "InvId": str(order_id), "SignatureValue": sig_pwd2}
    )
    assert res_result.status_code == 200

    # 2. Browser redirect to Success URL
    res_success = client.get(
        "/api/payment/robokassa/success",
        params={"OutSum": "300.00", "InvId": str(order_id), "SignatureValue": sig_pwd1}
    )
    assert res_success.status_code == 200
    data = res_success.json()
    assert data["status"] == "paid"


# --- TEST 6: UNKNOWN ORDER HANDLING ---
def test_unknown_order_not_found(client):
    res = client.post("/api/payment/create", json={"order_id": 999999})
    assert res.status_code == 404
    assert "Order not found" in res.json()["detail"]

    sig = compute_robokassa_sig("300.00", 999999, TEST_PASSWORD2)
    res_cb = client.post(
        "/api/payment/robokassa/result",
        params={"OutSum": "300.00", "InvId": "999999", "SignatureValue": sig}
    )
    assert res_cb.status_code == 404
