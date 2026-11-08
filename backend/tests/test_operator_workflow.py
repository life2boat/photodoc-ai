"""Operator workflow: authentication, authorization, download, upload, delivery."""
import io
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main  # noqa: E402
import operator_workflow as operator_service  # noqa: E402

TOKEN = "test-operator-token-not-a-real-secret"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.db")
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path / "uploads")
    main.UPLOAD_DIR.mkdir()
    monkeypatch.setenv("UPLOAD_DIR", str(main.UPLOAD_DIR))
    monkeypatch.setenv("OPERATOR_TOKEN", TOKEN)
    monkeypatch.setenv("PHOTODOC_ALLOW_FRESH_DB", "1")
    main.init_db()
    client = TestClient(main.app)
    return tmp_path, client


def add_order(order_id=1, status="paid", source=True):
    """Create an order and (optionally) its original file plus the delivery job row."""
    with sqlite3.connect(main.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO orders (id, name, phone, email, format, paper, crop, status,"
            " payment_status, order_amount, payment_amount, paid_at, created_at)"
            " VALUES (?, 'T', '000', 't@example.invalid', '3x4', 'glossy', 'none', ?, ?,"
            " '300.00', '300.00', ?, ?)",
            (order_id, "paid" if status == "paid" else "new", status,
             datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()),
        )
        if source:
            path = main.UPLOAD_DIR / f"{order_id}_source.jpg"
            path.write_bytes(JPEG)
            conn.execute(
                "INSERT INTO delivery_jobs (id, order_id, kind, local_path, remote_path,"
                " state, next_attempt_at, created_at) VALUES (?, ?, 'file', ?, ?, 'pending', ?, ?)",
                (f"job-{order_id}", order_id, str(path), f"r/{order_id}.jpg",
                 datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()),
            )
    return main.UPLOAD_DIR / f"{order_id}_source.jpg"


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_operator_disabled_without_configuration(env, monkeypatch):
    _, client = env
    monkeypatch.delenv("OPERATOR_TOKEN", raising=False)
    assert client.get("/api/operator/orders", headers=auth()).status_code == 503


def test_authentication_required(env):
    _, client = env
    assert client.get("/api/operator/orders").status_code == 401
    assert client.get("/api/operator/orders", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_list_returns_only_paid_orders(env):
    _, client = env
    add_order(1, "paid")
    add_order(2, "unpaid")
    payload = client.get("/api/operator/orders", headers=auth()).json()
    assert [o["id"] for o in payload["orders"]] == [1]


def test_unpaid_order_source_is_denied(env):
    _, client = env
    add_order(2, "unpaid")
    assert client.get("/api/operator/orders/2/source", headers=auth()).status_code == 409


def test_source_download_returns_the_original(env):
    _, client = env
    add_order(1)
    response = client.get("/api/operator/orders/1/source", headers=auth())
    assert response.status_code == 200
    assert response.content == JPEG


def test_missing_source_is_reported(env):
    _, client = env
    path = add_order(1)
    path.unlink()
    assert client.get("/api/operator/orders/1/source", headers=auth()).status_code == 410


def test_quarantined_source_requires_manual_recovery(env):
    _, client = env
    path = add_order(1)
    path.unlink()
    quarantine = main.UPLOAD_DIR / ".quarantine"
    quarantine.mkdir()
    (quarantine / "job-1.deadbeef.claim").write_bytes(JPEG)
    response = client.get("/api/operator/orders/1/source", headers=auth())
    assert response.status_code == 409
    assert "recovery" in response.json()["detail"].lower()


def test_valid_result_upload(env):
    _, client = env
    add_order(1)
    response = client.post("/api/operator/orders/1/result", headers=auth(),
                           files={"file": ("result.jpg", JPEG, "image/jpeg")})
    assert response.status_code == 200
    assert list(main.UPLOAD_DIR.glob("1_result_*"))


def test_invalid_image_is_rejected(env):
    _, client = env
    add_order(1)
    response = client.post("/api/operator/orders/1/result", headers=auth(),
                           files={"file": ("result.jpg", b"not an image", "image/jpeg")})
    assert response.status_code == 400


def test_unsupported_type_is_rejected(env):
    _, client = env
    add_order(1)
    response = client.post("/api/operator/orders/1/result", headers=auth(),
                           files={"file": ("result.gif", b"GIF89a" + b"0" * 32, "image/gif")})
    assert response.status_code == 415


def test_oversized_result_is_rejected(env, monkeypatch):
    _, client = env
    add_order(1)
    monkeypatch.setattr(operator_service, "MAX_RESULT_BYTES", 10)
    response = client.post("/api/operator/orders/1/result", headers=auth(),
                           files={"file": ("result.jpg", JPEG, "image/jpeg")})
    assert response.status_code == 413


def test_duplicate_upload_requires_explicit_replace(env):
    _, client = env
    add_order(1)
    first = client.post("/api/operator/orders/1/result", headers=auth(),
                        files={"file": ("result.jpg", JPEG, "image/jpeg")})
    assert first.status_code == 200
    second = client.post("/api/operator/orders/1/result", headers=auth(),
                         files={"file": ("result.jpg", JPEG, "image/jpeg")})
    assert second.status_code == 409
    replaced = client.post("/api/operator/orders/1/result?replace=true", headers=auth(),
                           files={"file": ("result.jpg", JPEG, "image/jpeg")})
    assert replaced.status_code == 200


def test_delivered_result_cannot_be_replaced(env):
    _, client = env
    add_order(1)
    client.post("/api/operator/orders/1/result", headers=auth(),
                files={"file": ("result.jpg", JPEG, "image/jpeg")})
    client.post("/api/operator/orders/1/submit", headers=auth())
    with sqlite3.connect(main.DB_PATH) as conn:
        conn.execute("UPDATE delivery_jobs SET state='delivered' "
                     "WHERE order_id=1 AND remote_path LIKE '%_result.bin'")
    assert client.post("/api/operator/orders/1/result", headers=auth(),
                       files={"file": ("result.jpg", JPEG, "image/jpeg")}).status_code == 409


def test_delivered_source_does_not_block_result_upload(env):
    """H-01 regression: a legacy delivered original must not complete the order."""
    _, client = env
    add_order(1)
    with sqlite3.connect(main.DB_PATH) as conn:
        conn.execute("UPDATE delivery_jobs SET state='delivered' WHERE order_id=1")
    assert client.post("/api/operator/orders/1/result", headers=auth(),
                       files={"file": ("result.jpg", JPEG, "image/jpeg")}).status_code == 200


def test_submit_requires_a_result(env):
    _, client = env
    add_order(1)
    assert client.post("/api/operator/orders/1/submit", headers=auth()).status_code == 409


def test_submit_is_idempotent_for_the_same_result(env):
    _, client = env
    add_order(1)
    client.post("/api/operator/orders/1/result", headers=auth(),
                files={"file": ("result.jpg", JPEG, "image/jpeg")})
    first = client.post("/api/operator/orders/1/submit", headers=auth())
    second = client.post("/api/operator/orders/1/submit", headers=auth())
    assert first.status_code == 200 and second.status_code == 200
    with sqlite3.connect(main.DB_PATH) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM delivery_jobs WHERE order_id=1 AND kind='file' "
            "AND state='pending' AND remote_path LIKE '%_result%'"
        ).fetchone()[0]
    assert count == 1, "a repeated submit created a second delivery job"


def test_delivery_state_is_observable(env):
    _, client = env
    add_order(1)
    payload = client.get("/api/operator/orders/1/delivery", headers=auth()).json()
    assert payload["jobs"][0]["state"] == "pending"


def test_audit_records_actions_without_secrets(env):
    _, client = env
    add_order(1)
    client.post("/api/operator/orders/1/result", headers=auth(),
                files={"file": ("result.jpg", JPEG, "image/jpeg")})
    client.post("/api/operator/orders/1/submit", headers=auth())
    with sqlite3.connect(main.DB_PATH) as conn:
        rows = conn.execute("SELECT event, detail FROM operator_audit").fetchall()
    events = [row[0] for row in rows]
    assert "result_uploaded" in events and "delivery_submitted" in events
    joined = " ".join(row[1] or "" for row in rows)
    assert TOKEN not in joined and "420" not in joined


def test_original_is_never_modified_by_the_workflow(env):
    _, client = env
    path = add_order(1)
    before = path.read_bytes()
    client.get("/api/operator/orders/1/source", headers=auth())
    client.post("/api/operator/orders/1/result", headers=auth(),
                files={"file": ("result.jpg", JPEG, "image/jpeg")})
    assert path.read_bytes() == before, "the operator workflow altered the source image"


def test_order_id_must_be_numeric(env):
    _, client = env
    add_order(1)
    assert client.get("/api/operator/orders/abc/source", headers=auth()).status_code == 422
