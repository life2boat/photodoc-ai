"""Comprehensive regression suite for real order source image lifecycle.
Contract: PHOTODOC-R1-REAL-ORDER-SOURCE-LIFECYCLE-FIX-01
Verifies Invariants A through F across the full 16-step real order flow
and adversarial edge cases (C-01, C-02, replacement races, callbacks).
"""
import hashlib
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
from fastapi.testclient import TestClient

import consents
import delivery
import main
import operator_workflow

SYNTHETIC_MERCHANT = "synthetic-merchant"
SYNTHETIC_PASS1 = "synthetic-pass-1"
SYNTHETIC_PASS2 = "synthetic-pass-2"
OPERATOR_TOKEN = "synthetic-operator-token"

SOURCE_BYTES = b"\xff\xd8\xff\xe0" + b"REAL-SOURCE-ORIGINAL-" * 8
SOURCE_SHA = hashlib.sha256(SOURCE_BYTES).hexdigest()

RESULT_BYTES = b"\xff\xd8\xff\xe0" + b"PROCESSED-RESULT-IMAGE-" * 8
RESULT_SHA = hashlib.sha256(RESULT_BYTES).hexdigest()

REPLACED_RESULT_BYTES = b"\xff\xd8\xff\xe0" + b"REPLACED-PROCESSED-IMAGE-" * 8
REPLACED_RESULT_SHA = hashlib.sha256(REPLACED_RESULT_BYTES).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "orders.db"
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads_dir)
    delivery.set_uploads_dir(uploads_dir)
    monkeypatch.setenv("UPLOAD_DIR", str(uploads_dir))
    monkeypatch.setenv("DELIVERY_WORKER_ENABLED", "0")
    monkeypatch.setenv("PHOTODOC_ALLOW_FRESH_DB", "1")
    monkeypatch.setenv("ROBOKASSA_MERCHANT_LOGIN", SYNTHETIC_MERCHANT)
    monkeypatch.setenv("ROBOKASSA_PASSWORD1", SYNTHETIC_PASS1)
    monkeypatch.setenv("ROBOKASSA_PASSWORD2", SYNTHETIC_PASS2)
    monkeypatch.setenv("ROBOKASSA_IS_TEST", "1")
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("YANDEX_DISK_TOKEN", "")
    monkeypatch.setenv("YANDEX_DISK_STAGING_TOKEN", "")
    monkeypatch.setenv("SMTP_PASSWORD", "")
    monkeypatch.setenv("YANDEX_DISK_ROOT_DIR", "Synthetic_Orders")
    monkeypatch.setenv("CONSENT_FEATURE_ENABLED", "1")
    monkeypatch.setitem(consents.DOCUMENTS[consents.PURPOSE_SERVICE], "approved", True)
    monkeypatch.setitem(consents.DOCUMENTS[consents.PURPOSE_PERSONAL_DATA], "approved", True)

    main.init_db()
    client = TestClient(main.app)
    return client, db_path, uploads_dir


def auth_header():
    return {"Authorization": f"Bearer {OPERATOR_TOKEN}"}


def calc_payment_signature(amount: str, order_id: int) -> str:
    # Robokassa callback uses Password#2
    return hashlib.md5(f"{amount}:{order_id}:{SYNTHETIC_PASS2}".encode()).hexdigest()


def test_16_step_real_order_source_lifecycle(env):
    """
    Mandatory 16-step regression scenario verifying Invariants A through F
    on the real application order-creation and payment path.
    """
    client, db_path, uploads_dir = env

    # Step 1: Create a real application order via POST /api/order
    # Step 2: Upload a synthetic original
    order_resp = client.post(
        "/api/order",
        data={
            "name": "Ivan Testov",
            "phone": "+79991234567",
            "email": "ivan@example.invalid",
            "format": "DOC_3X4",
            "paper": "Цифровая обработка",
            "crop": "По стандарту документа",
            "comment": "Срочно для паспорта",
            "consent_service_terms": "true",
            "consent_personal_data": "true",
        },
        files={"files": ("source.jpg", SOURCE_BYTES, "image/jpeg")},
    )
    assert order_resp.status_code == 200, f"Order creation failed: {order_resp.text}"
    order_data = order_resp.json()
    order_id = order_data["order_id"]
    amount = order_data["amount"]

    with sqlite3.connect(db_path) as conn:
        source_job = conn.execute(
            "SELECT id, local_path, remote_path, state, content_sha256, artifact_role "
            "FROM delivery_jobs WHERE order_id=? AND kind='file'",
            (order_id,),
        ).fetchone()
    assert source_job is not None, "Source job was not enqueued"
    source_job_id = source_job[0]
    source_local_path = Path(source_job[1])
    assert source_job[4] == SOURCE_SHA, "Source SHA mismatch in DB"
    assert source_job[5] == "source", f"Expected artifact_role='source', got {source_job[5]}"
    assert source_local_path.is_file(), "Source file not found on disk"
    assert source_local_path.read_bytes() == SOURCE_BYTES

    # Step 3: Simulate a valid signed payment callback via POST /api/payment/robokassa/result
    sig = calc_payment_signature(amount, order_id)
    pay_resp = client.post(
        "/api/payment/robokassa/result",
        data={
            "OutSum": amount,
            "InvId": str(order_id),
            "SignatureValue": sig,
        },
    )
    assert pay_resp.status_code == 200, f"Payment callback failed: {pay_resp.text}"
    assert pay_resp.text.startswith(f"OK{order_id}")

    with sqlite3.connect(db_path) as conn:
        order_row = conn.execute("SELECT payment_status FROM orders WHERE id=?", (order_id,)).fetchone()
    assert order_row[0] == "paid", "Order payment_status is not 'paid'"

    # Step 4: Run claim_due
    # Step 5: Assert the original is NOT claimable for customer delivery (Invariant A)
    claimed_jobs = []
    while True:
        job = delivery.claim_due(db_path)
        if job is None:
            break
        claimed_jobs.append(job)
        # Finalize non-file jobs (e.g. info or paid_email) so worker progresses
        delivery.finish(db_path, job["id"], lease_until=job.get("lease_until"))

    claimed_ids = [j["id"] for j in claimed_jobs]
    assert source_job_id not in claimed_ids, (
        f"INVARIANT A VIOLATION: Source job {source_job_id} was claimed for customer delivery!"
    )
    for j in claimed_jobs:
        if j["kind"] == "file":
            assert j.get("artifact_role") != "source", "Claimed job must not be a source artifact"

    # Step 6: Run delivery cleanup
    delivery.process_due_cleanups(db_path, uploads_dir=uploads_dir)

    # Step 7: Assert the original still exists (Invariant C)
    assert source_local_path.is_file(), "INVARIANT C VIOLATION: Original file was deleted by delivery cleanup!"
    assert source_local_path.read_bytes() == SOURCE_BYTES

    # Step 8: Authenticate operator
    # Step 9: Download original through /source
    source_resp = client.get(
        f"/api/operator/orders/{order_id}/source",
        headers=auth_header(),
    )
    assert source_resp.status_code == 200, f"Operator source download failed: {source_resp.status_code}"

    # Step 10: Verify original SHA-256
    assert source_resp.content == SOURCE_BYTES
    assert hashlib.sha256(source_resp.content).hexdigest() == SOURCE_SHA

    # Step 11: Upload a distinct synthetic processed result via POST /result
    upload_result_resp = client.post(
        f"/api/operator/orders/{order_id}/result",
        headers=auth_header(),
        files={"file": ("processed_result.jpg", RESULT_BYTES, "image/jpeg")},
    )
    assert upload_result_resp.status_code == 200, f"Result upload failed: {upload_result_resp.text}"

    # Step 12: Submit it for delivery via POST /submit
    submit_resp = client.post(
        f"/api/operator/orders/{order_id}/submit",
        headers=auth_header(),
    )
    assert submit_resp.status_code == 200, f"Result submit failed: {submit_resp.text}"
    result_job_id = submit_resp.json()["job_id"]

    # Step 13: Assert claim_due returns ONLY the processed result (Invariant D)
    result_claim = delivery.claim_due(db_path)
    assert result_claim is not None, "claim_due did not return the queued processed result"
    assert result_claim["id"] == result_job_id, f"Expected job {result_job_id}, got {result_claim['id']}"
    assert result_claim["kind"] == "file"
    assert result_claim["artifact_role"] == "result"
    assert result_claim["id"] != source_job_id, "Claimed job must not be the source job"

    # Step 14: Verify the queued artifact hash
    assert result_claim["content_sha256"] == RESULT_SHA
    result_local_path = Path(result_claim["local_path"])
    assert result_local_path.is_file()
    assert hashlib.sha256(result_local_path.read_bytes()).hexdigest() == RESULT_SHA

    # Step 15: Simulate successful customer delivery
    commit_ok = delivery.finish(db_path, result_job_id, lease_until=result_claim["lease_until"])
    assert commit_ok is True, "finish() delivery commitment failed"

    # Worker executes cleanup of the delivered result
    delivery.perform_job_cleanup(db_path, result_job_id, uploads_dir=uploads_dir)

    # Step 16: Assert original and processed result remain correctly distinguished (Invariant E)
    # The original source file must STILL exist on disk and remain accessible via /source
    assert source_local_path.is_file(), "Original source file was deleted after result delivery!"
    source_after = client.get(
        f"/api/operator/orders/{order_id}/source",
        headers=auth_header(),
    )
    assert source_after.status_code == 200
    assert source_after.content == SOURCE_BYTES, "INVARIANT E VIOLATION: /source returned wrong content!"
    assert hashlib.sha256(source_after.content).hexdigest() == SOURCE_SHA


def test_duplicate_payment_callback_is_idempotent_and_preserves_source(env):
    """Duplicate payment callbacks must not alter jobs or corrupt the source."""
    client, db_path, uploads_dir = env
    order_resp = client.post(
        "/api/order",
        data={
            "name": "Dup Callback", "phone": "123", "format": "10x15",
            "consent_service_terms": "true", "consent_personal_data": "true",
        },
        files={"files": ("orig.jpg", SOURCE_BYTES, "image/jpeg")},
    )
    order_id = order_resp.json()["order_id"]
    amount = order_resp.json()["amount"]
    sig = calc_payment_signature(amount, order_id)

    # First callback
    cb1 = client.post("/api/payment/robokassa/result", data={"OutSum": amount, "InvId": str(order_id), "SignatureValue": sig})
    assert cb1.status_code == 200

    # Second duplicate callback
    cb2 = client.post("/api/payment/robokassa/result", data={"OutSum": amount, "InvId": str(order_id), "SignatureValue": sig})
    assert cb2.status_code == 200

    # Verify jobs count: should have 1 source file, 1 info, 1 admin_email, 1 paid_email
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        jobs = conn.execute("SELECT kind, artifact_role, state FROM delivery_jobs WHERE order_id=?", (order_id,)).fetchall()
    kinds = [f"{j['kind']}:{j['artifact_role']}" for j in jobs]
    assert kinds.count("file:source") == 1
    assert kinds.count("paid_email:None") == 1

    # Source remains downloadable
    src = client.get(f"/api/operator/orders/{order_id}/source", headers=auth_header())
    assert src.status_code == 200
    assert src.content == SOURCE_BYTES


def test_delayed_payment_callback_preserves_source(env):
    """Payment arriving after delay still allows normal operator download."""
    client, db_path, _ = env
    order_resp = client.post(
        "/api/order",
        data={
            "name": "Delayed Pay", "phone": "123", "format": "10x15",
            "consent_service_terms": "true", "consent_personal_data": "true",
        },
        files={"files": ("orig.jpg", SOURCE_BYTES, "image/jpeg")},
    )
    order_id = order_resp.json()["order_id"]
    amount = order_resp.json()["amount"]

    # Before payment: operator cannot download (409)
    assert client.get(f"/api/operator/orders/{order_id}/source", headers=auth_header()).status_code == 409

    # Payment arrives
    sig = calc_payment_signature(amount, order_id)
    cb = client.post("/api/payment/robokassa/result", data={"OutSum": amount, "InvId": str(order_id), "SignatureValue": sig})
    assert cb.status_code == 200

    # Now downloadable
    src = client.get(f"/api/operator/orders/{order_id}/source", headers=auth_header())
    assert src.status_code == 200
    assert src.content == SOURCE_BYTES


def test_quarantined_source_returns_409_and_is_not_claimable(env):
    """Source in quarantine (C-01 state) returns 409 on /source and cannot be claimed."""
    client, db_path, uploads_dir = env
    order_resp = client.post(
        "/api/order",
        data={
            "name": "Quarantine User", "phone": "123", "format": "10x15",
            "consent_service_terms": "true", "consent_personal_data": "true",
        },
        files={"files": ("orig.jpg", SOURCE_BYTES, "image/jpeg")},
    )
    order_id = order_resp.json()["order_id"]
    amount = order_resp.json()["amount"]
    sig = calc_payment_signature(amount, order_id)
    client.post("/api/payment/robokassa/result", data={"OutSum": amount, "InvId": str(order_id), "SignatureValue": sig})

    with sqlite3.connect(db_path) as conn:
        job_id = conn.execute("SELECT id, local_path FROM delivery_jobs WHERE order_id=? AND kind='file'", (order_id,)).fetchone()[0]

    # Simulate quarantine
    quarantine = uploads_dir / ".quarantine"
    quarantine.mkdir(exist_ok=True)
    claim_file = quarantine / f"{job_id}.deadbeef.claim"
    claim_file.write_bytes(SOURCE_BYTES)

    # Local file unlinked
    Path(conn.execute("SELECT local_path FROM delivery_jobs WHERE id=?", (job_id,)).fetchone()[0]).unlink()

    # /source returns 409 with recovery detail
    resp = client.get(f"/api/operator/orders/{order_id}/source", headers=auth_header())
    assert resp.status_code == 409
    assert "recovery" in resp.json()["detail"].lower()

    # claim_due must not claim it
    claim = delivery.claim_due(db_path)
    if claim is not None:
        assert claim["id"] != job_id


def test_missing_original_returns_410(env):
    """Missing original without quarantine returns 410."""
    client, db_path, _ = env
    order_resp = client.post(
        "/api/order",
        data={
            "name": "Missing User", "phone": "123", "format": "10x15",
            "consent_service_terms": "true", "consent_personal_data": "true",
        },
        files={"files": ("orig.jpg", SOURCE_BYTES, "image/jpeg")},
    )
    order_id = order_resp.json()["order_id"]
    amount = order_resp.json()["amount"]
    sig = calc_payment_signature(amount, order_id)
    client.post("/api/payment/robokassa/result", data={"OutSum": amount, "InvId": str(order_id), "SignatureValue": sig})

    with sqlite3.connect(db_path) as conn:
        path = Path(conn.execute("SELECT local_path FROM delivery_jobs WHERE order_id=? AND kind='file'", (order_id,)).fetchone()[0])
    path.unlink()

    resp = client.get(f"/api/operator/orders/{order_id}/source", headers=auth_header())
    assert resp.status_code == 410


def test_result_replacement_and_delivery_in_place(env):
    """H-02: replacing result updates delivery job in place with new hash."""
    client, db_path, uploads_dir = env
    order_resp = client.post(
        "/api/order",
        data={
            "name": "Replace User", "phone": "123", "format": "10x15",
            "consent_service_terms": "true", "consent_personal_data": "true",
        },
        files={"files": ("orig.jpg", SOURCE_BYTES, "image/jpeg")},
    )
    order_id = order_resp.json()["order_id"]
    amount = order_resp.json()["amount"]
    sig = calc_payment_signature(amount, order_id)
    client.post("/api/payment/robokassa/result", data={"OutSum": amount, "InvId": str(order_id), "SignatureValue": sig})

    # Upload first result
    up1 = client.post(
        f"/api/operator/orders/{order_id}/result",
        headers=auth_header(),
        files={"file": ("res1.jpg", RESULT_BYTES, "image/jpeg")},
    )
    assert up1.status_code == 200

    # Submit first result
    sub1 = client.post(f"/api/operator/orders/{order_id}/submit", headers=auth_header())
    assert sub1.status_code == 200
    job1_id = sub1.json()["job_id"]

    # Attempt second upload without replace=true -> 409
    up2_fail = client.post(
        f"/api/operator/orders/{order_id}/result",
        headers=auth_header(),
        files={"file": ("res2.jpg", REPLACED_RESULT_BYTES, "image/jpeg")},
    )
    assert up2_fail.status_code == 409

    # Second upload with replace=true -> 200
    up2 = client.post(
        f"/api/operator/orders/{order_id}/result?replace=true",
        headers=auth_header(),
        files={"file": ("res2.jpg", REPLACED_RESULT_BYTES, "image/jpeg")},
    )
    assert up2.status_code == 200

    # Re-submit -> updates same job in place
    sub2 = client.post(f"/api/operator/orders/{order_id}/submit", headers=auth_header())
    assert sub2.status_code == 200
    job2_id = sub2.json()["job_id"]
    assert job1_id == job2_id, "Replacement must update existing delivery job in place"

    # Worker claims updated job
    claimed = delivery.claim_due(db_path)
    # Drain non-result jobs if any
    while claimed and claimed["id"] != job2_id:
        delivery.finish(db_path, claimed["id"], lease_until=claimed.get("lease_until"))
        claimed = delivery.claim_due(db_path)

    assert claimed is not None
    assert claimed["id"] == job2_id
    assert claimed["content_sha256"] == REPLACED_RESULT_SHA


def test_worker_claim_racing_with_result_replacement(env):
    """When result delivery job is in 'processing', replacement upload is rejected (409)."""
    client, db_path, _ = env
    order_resp = client.post(
        "/api/order",
        data={
            "name": "Race User", "phone": "123", "format": "10x15",
            "consent_service_terms": "true", "consent_personal_data": "true",
        },
        files={"files": ("orig.jpg", SOURCE_BYTES, "image/jpeg")},
    )
    order_id = order_resp.json()["order_id"]
    amount = order_resp.json()["amount"]
    sig = calc_payment_signature(amount, order_id)
    client.post("/api/payment/robokassa/result", data={"OutSum": amount, "InvId": str(order_id), "SignatureValue": sig})

    client.post(
        f"/api/operator/orders/{order_id}/result",
        headers=auth_header(),
        files={"file": ("res.jpg", RESULT_BYTES, "image/jpeg")},
    )
    sub = client.post(f"/api/operator/orders/{order_id}/submit", headers=auth_header())
    job_id = sub.json()["job_id"]

    # Worker claims result job -> state becomes 'processing'
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE delivery_jobs SET state='processing', lease_until='2099-01-01T00:00:00' WHERE id=?", (job_id,))

    # Replacement upload must be rejected with 409
    replace_resp = client.post(
        f"/api/operator/orders/{order_id}/result?replace=true",
        headers=auth_header(),
        files={"file": ("new.jpg", REPLACED_RESULT_BYTES, "image/jpeg")},
    )
    assert replace_resp.status_code == 409
    assert "already queued or delivered" in replace_resp.json()["detail"].lower()


def test_duplicate_delivery_submission_is_idempotent(env):
    """Repeated submit calls return 200 and do not spawn duplicate jobs."""
    client, db_path, _ = env
    order_resp = client.post(
        "/api/order",
        data={
            "name": "Idempotent Submit", "phone": "123", "format": "10x15",
            "consent_service_terms": "true", "consent_personal_data": "true",
        },
        files={"files": ("orig.jpg", SOURCE_BYTES, "image/jpeg")},
    )
    order_id = order_resp.json()["order_id"]
    amount = order_resp.json()["amount"]
    sig = calc_payment_signature(amount, order_id)
    client.post("/api/payment/robokassa/result", data={"OutSum": amount, "InvId": str(order_id), "SignatureValue": sig})

    client.post(
        f"/api/operator/orders/{order_id}/result",
        headers=auth_header(),
        files={"file": ("res.jpg", RESULT_BYTES, "image/jpeg")},
    )

    s1 = client.post(f"/api/operator/orders/{order_id}/submit", headers=auth_header())
    s2 = client.post(f"/api/operator/orders/{order_id}/submit", headers=auth_header())
    assert s1.status_code == 200 and s2.status_code == 200
    assert s1.json()["job_id"] == s2.json()["job_id"]

    with sqlite3.connect(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM delivery_jobs WHERE order_id=? AND artifact_role='result'",
            (order_id,),
        ).fetchone()[0]
    assert count == 1
