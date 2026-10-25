"""P1 HTTP and outbox tests. No external API or customer data is used."""
import hashlib
import os
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
import main  # noqa: E402
from delivery import claim_due, finish, process_due, utc_now  # noqa: E402
from migrations.reconcile_delivery_schema import reconcile  # noqa: E402

JPEG = b"\xff\xd8\xffsynthetic-image"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    db = tmp_path / "orders.db"
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(main, "DB_PATH", db)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads)
    monkeypatch.setenv("ROBOKASSA_MERCHANT_LOGIN", "test-merchant")
    monkeypatch.setenv("ROBOKASSA_PASSWORD1", "test-pass-1")
    monkeypatch.setenv("ROBOKASSA_PASSWORD2", "test-pass-2")
    monkeypatch.setenv("DELIVERY_WORKER_ENABLED", "0")
    main.init_db()
    yield TestClient(main.app), db, uploads


def create(client, format_name="10x15", paper="Матовая", crop="Без полей", names=("one.jpg",)):
    return client.post("/api/order", data={
        "name": "Test Client", "phone": "79990000000", "email": "client@example.invalid",
        "format": format_name, "paper": paper, "crop": crop,
    }, files=[("files", (name, JPEG, "image/jpeg")) for name in names])


def jobs(db, order_id):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(
            "SELECT * FROM delivery_jobs WHERE order_id=? ORDER BY kind, id", (order_id,)
        )]


@pytest.mark.parametrize("format_name,paper,crop,expected", [
    ("DOC_3X4", "Цифровая обработка", "По стандарту документа", "300.00"),
    ("10x15", "Матовая", "Без полей", "20.00"),
    ("Реставрация фото", "Цифровая обработка", "Легкая реставрация", "200.00"),
    ("Polaroid", "Глянцевая", "С рамкой", "30.00"),
])
def test_four_forms_create_order_and_outbox(isolated, format_name, paper, crop, expected):
    client, db, _ = isolated
    response = create(client, format_name, paper, crop)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["amount"] == expected and len(result["order_token"]) >= 32
    with sqlite3.connect(db) as conn:
        stored_hash = conn.execute("SELECT order_token_hash FROM orders WHERE id=?", (result["order_id"],)).fetchone()[0]
    assert stored_hash == hashlib.sha256(result["order_token"].encode()).hexdigest()
    assert result["order_token"] != stored_hash
    kinds = sorted(job["kind"] for job in jobs(db, result["order_id"]))
    assert kinds == ["admin_email", "file", "info"]


def test_duplicate_names_use_distinct_local_and_remote_ids(isolated):
    client, db, uploads = isolated
    response = create(client, names=("same.jpg", "same.jpg"))
    assert response.status_code == 200, response.text
    order_id = response.json()["order_id"]
    file_jobs = [job for job in jobs(db, order_id) if job["kind"] == "file"]
    assert len({job["local_path"] for job in file_jobs}) == 2
    assert len({job["remote_path"] for job in file_jobs}) == 2
    assert {job["original_name"] for job in file_jobs} == {"same.jpg"}
    assert all(Path(job["local_path"]).is_file() for job in file_jobs)
    assert all(job["remote_path"].startswith(f"PhotoDoc_Orders/Заказ_{order_id}_Test_Client/10x15_Матовая_Без_полей/") for job in file_jobs)
    assert len(list(uploads.glob("*.jpg"))) == 2


def test_invalid_type_limit_and_interrupted_upload_leave_no_order(isolated):
    client, db, uploads = isolated
    wrong_magic = client.post("/api/order", data={"name": "Test", "phone": "7999", "format": "10x15"},
                              files={"files": ("bad.jpg", b"not-an-image", "image/jpeg")})
    assert wrong_magic.status_code == 400
    with patch.object(main, "MAX_FILE_BYTES", 5):
        oversized = create(client)
    assert oversized.status_code == 413
    too_many = create(client, names=tuple(f"{i}.jpg" for i in range(main.MAX_FILES + 1)))
    assert too_many.status_code == 400
    with patch.object(main, "_stage_upload", side_effect=OSError("interrupted stream")):
        interrupted = create(client)
    assert interrupted.status_code == 500
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert list(uploads.iterdir()) == []


def test_failed_partial_delivery_and_restart_retry_keep_sources(isolated):
    client, db, _ = isolated
    result = create(client, names=("a.jpg", "b.jpg")).json()
    order_id = result["order_id"]
    initial = jobs(db, order_id)
    file_jobs = [job for job in initial if job["kind"] == "file"]
    first_file = file_jobs[0]["id"]
    delivered = []

    def partial(job):
        if job["id"] == first_file:
            delivered.append(job["id"])
        elif job["kind"] in ("file", "info"):
            raise TimeoutError("synthetic Yandex timeout")

    process_due(db, partial)
    current = jobs(db, order_id)
    assert next(job for job in current if job["id"] == first_file)["state"] == "delivered"
    assert any(job["state"] == "failed" for job in current)
    assert all(Path(job["local_path"]).exists() for job in current if job["kind"] in ("file", "info"))

    # Simulate a new backend process after its backoff elapsed: same persisted DB.
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE delivery_jobs SET next_attempt_at=? WHERE state='failed'", (utc_now(),))
    retried = []
    process_due(db, lambda job: retried.append(job["id"]))
    assert len(retried) >= 2
    assert all(job["state"] == "delivered" for job in jobs(db, order_id))


def test_expired_lease_recovered_after_crash(isolated):
    client, db, _ = isolated
    order_id = create(client).json()["order_id"]
    claimed = claim_due(db)
    assert claimed is not None
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE delivery_jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?", (claimed["id"],))
    seen = []
    process_due(db, lambda job: seen.append(job["id"]), limit=10)
    assert claimed["id"] in seen
    assert all(job["state"] == "delivered" for job in jobs(db, order_id))


def test_backend_startup_worker_resumes_persisted_jobs(isolated, monkeypatch):
    client, db, _ = isolated
    order_id = create(client).json()["order_id"]
    seen = []
    monkeypatch.setenv("DELIVERY_WORKER_ENABLED", "1")
    with patch.object(main, "deliver_job", side_effect=lambda job: seen.append(job["id"])):
        with TestClient(main.app):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and any(job["state"] != "delivered" for job in jobs(db, order_id)):
                time.sleep(0.05)
    assert len(seen) == 3
    assert all(job["state"] == "delivered" for job in jobs(db, order_id))


def test_wrong_token_foreign_order_underpayment_paid_terminal(isolated):
    client, db, _ = isolated
    first = create(client).json()
    second = create(client, format_name="Polaroid", paper="Глянцевая", crop="С рамкой").json()
    endpoint = "/api/payment/create"
    assert client.post(endpoint, json={"order_id": first["order_id"], "order_token": first["order_token"], "amount": 0.01}).status_code == 400
    assert client.post(endpoint, json={"order_id": second["order_id"], "order_token": first["order_token"]}).status_code == 404
    assert client.post(endpoint, json={"order_id": first["order_id"], "order_token": "wrong"}).status_code == 404
    assert client.post(endpoint, json={"order_id": first["order_id"], "order_token": first["order_token"]}).status_code == 200
    import hashlib as _hashlib
    signature = _hashlib.md5(f"20.00:{first['order_id']}:test-pass-2".encode()).hexdigest()
    callback = "/api/payment/robokassa/result"
    data = {"OutSum": "20.00", "InvId": str(first["order_id"]), "SignatureValue": signature}
    assert client.post(callback, data={**data, "SignatureValue": "invalid"}).status_code == 400
    assert client.post(callback, data=data).status_code == 200
    assert client.post(callback, data=data).status_code == 200
    assert client.post(endpoint, json={"order_id": first["order_id"], "order_token": first["order_token"]}).status_code == 409
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT payment_status FROM orders WHERE id=?", (first["order_id"],)).fetchone()[0] == "paid"
        assert conn.execute("SELECT COUNT(*) FROM delivery_jobs WHERE order_id=? AND kind='paid_email'", (first["order_id"],)).fetchone()[0] == 1


def test_legacy_order_migration_keeps_row_and_blocks_new_payment(isolated):
    client, db, _ = isolated
    with sqlite3.connect(db) as conn:
        legacy_id = conn.execute("INSERT INTO orders (name, payment_status) VALUES ('Legacy', 'unpaid')").lastrowid
    assert client.post("/api/payment/create", json={"order_id": legacy_id, "order_token": "unknown"}).status_code == 409
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT name, payment_status FROM orders WHERE id=?", (legacy_id,)).fetchone() == ("Legacy", "unpaid")


def test_additive_migration_keeps_old_rows_and_files(tmp_path):
    db = tmp_path / "old-orders.db"
    old_file = tmp_path / "old-photo.jpg"
    old_file.write_bytes(JPEG)
    with sqlite3.connect(db) as conn:
        conn.execute(f"PRAGMA application_id={main.APPLICATION_ID}")
        conn.execute("CREATE TABLE orders (" + ", ".join(
            f"{column} TEXT" for column in (
                "id", "name", "phone", "email", "format", "paper", "crop", "comment", "filename",
                "status", "payment_status", "order_amount", "payment_amount", "payment_method",
                "robokassa_inv_id", "paid_at", "payment_email_sent_at", "created_at"
            )
        ) + ")")
        conn.execute("INSERT INTO orders (id, name, payment_status) VALUES ('7', 'Old Customer', 'paid')")
    assert reconcile(db)["migration_required"]
    assert reconcile(db, apply=True)["applied"]
    assert not reconcile(db)["migration_required"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT name, payment_status, order_token_hash FROM orders WHERE id='7'").fetchone() == (
            "Old Customer", "paid", None
        )
        assert conn.execute("SELECT COUNT(*) FROM delivery_jobs").fetchone()[0] == 0
    assert old_file.read_bytes() == JPEG
