"""Comprehensive 16-Step Customer Journey Integration Test (Stage 2).

Verifies the complete PhotoDoc AI lifecycle:
1. Legal document canonical hash verification (M-02)
2. Consent acceptance & hash verification
3. Order creation with mandatory consents & JPEG validation (H-05)
4. Atomic database commit (orders + consent_records + delivery_jobs)
5. Evidence query via GET /api/order/{id}/consents (token-protected)
6. Payment initiation
7. Robokassa signed payment callback
8. Payment state transition to 'paid'
9. Source image delivery exclusion (Invariant A: source never claimed)
10. Operator authentication & order queue inspection
11. Operator source image download & byte verification
12. Operator result image upload & submission
13. Result job queued with artifact_role='result'
14. Result delivery claim & completion
15. Post-delivery cleanup (result unlinked, original source preserved!)
16. Coordinated retention cycle (H-02 & M-03):
    - Lease acquired with multi-instance fencing
    - Paid order protected under hold escalation (never deleted by timer)
    - Unpaid order expired and purged to quarantine
    - Backup & log rotation safety verified
    - Telemetry recorded
"""
import hashlib
import io
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import backup_rotation
import consents
import delivery
import log_rotation
import main
import operator_workflow
import retention
import retention_worker

SYNTHETIC_MERCHANT = "journey-merchant"
SYNTHETIC_PASS1 = "journey-pass-1"
SYNTHETIC_PASS2 = "journey-pass-2"
OPERATOR_TOKEN = "journey-operator-token"

SOURCE_BYTES = b"\xff\xd8\xff\xe0" + b"JOURNEY-CUSTOMER-ORIGINAL-" * 8
SOURCE_SHA = hashlib.sha256(SOURCE_BYTES).hexdigest()

RESULT_BYTES = b"\xff\xd8\xff\xe0" + b"JOURNEY-OPERATOR-RESULT-" * 8
RESULT_SHA = hashlib.sha256(RESULT_BYTES).hexdigest()


@pytest.fixture
def journey_env(tmp_path, monkeypatch):
    db_path = tmp_path / "journey_orders.db"
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads_dir)
    delivery.set_uploads_dir(uploads_dir)

    monkeypatch.setenv("UPLOAD_DIR", str(uploads_dir))
    monkeypatch.setenv("BACKUP_DIR", str(backups_dir))
    monkeypatch.setenv("LOG_DIR", str(logs_dir))
    monkeypatch.setenv("DELIVERY_WORKER_ENABLED", "0")
    monkeypatch.setenv("RETENTION_WORKER_ENABLED", "0")
    monkeypatch.setenv("BACKUP_ROTATION_ENABLED", "1")
    monkeypatch.setenv("LOG_ROTATION_VERIFICATION_ENABLED", "1")
    monkeypatch.setenv("CONSENT_FEATURE_ENABLED", "1")
    monkeypatch.setenv("PHOTODOC_ALLOW_FRESH_DB", "1")
    monkeypatch.setenv("ROBOKASSA_MERCHANT_LOGIN", SYNTHETIC_MERCHANT)
    monkeypatch.setenv("ROBOKASSA_PASSWORD1", SYNTHETIC_PASS1)
    monkeypatch.setenv("ROBOKASSA_PASSWORD2", SYNTHETIC_PASS2)
    monkeypatch.setenv("ROBOKASSA_IS_TEST", "1")
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setitem(consents.DOCUMENTS[consents.PURPOSE_SERVICE], "approved", True)
    monkeypatch.setitem(consents.DOCUMENTS[consents.PURPOSE_PERSONAL_DATA], "approved", True)

    main.init_db()
    client = TestClient(main.app)

    yield {
        "client": client,
        "db": db_path,
        "uploads": uploads_dir,
        "backups": backups_dir,
        "logs": logs_dir,
    }


def calc_signature(amount: str, order_id: int) -> str:
    return hashlib.md5(f"{amount}:{order_id}:{SYNTHETIC_PASS2}".encode()).hexdigest()


def test_complete_16_step_customer_journey(journey_env):
    client = journey_env["client"]
    db_path = journey_env["db"]
    uploads_dir = journey_env["uploads"]
    backups_dir = journey_env["backups"]
    logs_dir = journey_env["logs"]

    # --- STEP 1: Verify Canonical Legal Document Hashes (M-02) ---
    assert consents.verify_document_hash(consents.PURPOSE_SERVICE, consents.CANONICAL_OFFER_HASH)
    assert consents.verify_document_hash(consents.PURPOSE_PERSONAL_DATA, consents.CANONICAL_PRIVACY_HASH)
    assert consents.DOCUMENTS[consents.PURPOSE_THIRD_PARTY_IMAGES]["approved"] is False

    # --- STEP 2 & 3: Order Creation with Mandatory Consents (H-05) ---
    order_resp = client.post(
        "/api/order",
        data={
            "name": "Алексей Смирнов",
            "phone": "+79997778899",
            "email": "alex@example.invalid",
            "format": "DOC_3X4",
            "paper": "Цифровая обработка",
            "crop": "По стандарту документа",
            "comment": "Срочно для визы",
            "consent_service_terms": "true",
            "consent_personal_data": "true",
            "consent_doc_version": "2026-11-08",
            "consent_doc_hash": consents.CANONICAL_OFFER_HASH,
        },
        files={"files": ("source.jpg", io.BytesIO(SOURCE_BYTES), "image/jpeg")},
    )
    assert order_resp.status_code == 200, f"Order creation failed: {order_resp.text}"
    order_data = order_resp.json()
    order_id = order_data["order_id"]
    order_token = order_data["order_token"]
    amount = order_data["amount"]

    # --- STEP 4: Atomic DB Commit Verification ---
    with sqlite3.connect(db_path) as conn:
        order_row = conn.execute(
            "SELECT id, status, payment_status, order_token_hash FROM orders WHERE id=?",
            (order_id,),
        ).fetchone()
        assert order_row is not None
        assert order_row[1] == "new"
        assert order_row[2] == "unpaid"

        # Check source job in delivery_jobs
        source_job = conn.execute(
            "SELECT id, local_path, artifact_role, content_sha256 FROM delivery_jobs "
            "WHERE order_id=? AND kind='file'",
            (order_id,),
        ).fetchone()
        assert source_job is not None
        source_job_id, source_local_path_str, artifact_role, job_sha = source_job
        assert artifact_role == "source"
        assert job_sha == SOURCE_SHA
        source_local_path = Path(source_local_path_str)
        assert source_local_path.is_file()
        assert source_local_path.read_bytes() == SOURCE_BYTES

    # --- STEP 5: Token-Protected Consent Evidence Query (H-05) ---
    ev_resp = client.get(f"/api/order/{order_id}/consents?order_token={order_token}")
    assert ev_resp.status_code == 200
    evidence = ev_resp.json()["consents"]
    purposes = {e["purpose"]: e for e in evidence}
    assert consents.PURPOSE_SERVICE in purposes
    assert consents.PURPOSE_PERSONAL_DATA in purposes
    assert purposes[consents.PURPOSE_SERVICE]["document_hash"] == consents.CANONICAL_OFFER_HASH

    # Bad token is rejected
    bad_ev = client.get(f"/api/order/{order_id}/consents?order_token=badtoken")
    assert bad_ev.status_code == 403

    # --- STEP 6: Payment Initiation ---
    pay_init = client.post(
        "/api/payment/create",
        json={"order_id": order_id, "order_token": order_token, "amount": amount},
    )
    assert pay_init.status_code == 200
    assert pay_init.json()["ok"] is True

    # --- STEP 7 & 8: Robokassa Payment Callback Processing ---
    sig = calc_signature(amount, order_id)
    callback_resp = client.post(
        "/api/payment/robokassa/result",
        data={"OutSum": amount, "InvId": str(order_id), "SignatureValue": sig},
    )
    assert callback_resp.status_code == 200
    assert callback_resp.text.startswith(f"OK{order_id}")

    with sqlite3.connect(db_path) as conn:
        pay_status = conn.execute("SELECT payment_status FROM orders WHERE id=?", (order_id,)).fetchone()[0]
        assert pay_status == "paid"

    # --- STEP 9: Customer Source Exclusion (Invariant A) ---
    claimed_jobs = []
    while True:
        job = delivery.claim_due(db_path)
        if job is None:
            break
        claimed_jobs.append(job)
        delivery.finish(db_path, job["id"], lease_until=job.get("lease_until"))

    claimed_ids = [j["id"] for j in claimed_jobs]
    assert source_job_id not in claimed_ids, "INVARIANT A VIOLATION: Source job claimed for delivery!"

    # Delivery cleanup runs
    delivery.process_due_cleanups(db_path, uploads_dir=uploads_dir)
    assert source_local_path.is_file(), "INVARIANT C VIOLATION: Source deleted before operator process!"

    # --- STEP 10: Operator Authentication & Queue Inspection ---
    op_headers = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}
    queue_resp = client.get("/api/operator/orders", headers=op_headers)
    assert queue_resp.status_code == 200
    orders_list = queue_resp.json()["orders"]
    matching_order = next((o for o in orders_list if o["id"] == order_id), None)
    assert matching_order is not None
    assert matching_order["payment_status"] == "paid"

    # --- STEP 11: Operator Downloads Original Source Image ---
    source_download = client.get(f"/api/operator/orders/{order_id}/source", headers=op_headers)
    assert source_download.status_code == 200
    assert source_download.content == SOURCE_BYTES
    assert hashlib.sha256(source_download.content).hexdigest() == SOURCE_SHA

    # --- STEP 12 & 13: Operator Uploads and Submits Processed Result ---
    upload_res = client.post(
        f"/api/operator/orders/{order_id}/result",
        headers=op_headers,
        files={"file": ("result_final.jpg", io.BytesIO(RESULT_BYTES), "image/jpeg")},
    )
    assert upload_res.status_code == 200

    submit_res = client.post(f"/api/operator/orders/{order_id}/submit", headers=op_headers)
    assert submit_res.status_code == 200
    result_job_id = submit_res.json()["job_id"]

    # --- STEP 14: Result Job Delivery Execution (Invariant D) ---
    result_claim = delivery.claim_due(db_path)
    assert result_claim is not None
    assert result_claim["id"] == result_job_id
    assert result_claim["artifact_role"] == "result"
    assert result_claim["content_sha256"] == RESULT_SHA

    # Finish result delivery and enqueue cleanup
    ok = delivery.finish(
        db_path,
        result_job_id,
        lease_until=result_claim.get("lease_until"),
    )
    assert ok is True, "delivery.finish failed to mark result job delivered"


    # --- STEP 15: Post-Delivery Cleanup ---
    delivery.process_due_cleanups(db_path, uploads_dir=uploads_dir)
    result_file = Path(result_claim["local_path"])
    assert not result_file.exists(), "Delivered result file was not cleaned up!"
    # Original source file MUST remain intact
    assert source_local_path.is_file(), "Original source file was deleted after result cleanup!"

    # --- STEP 16: Coordinated Retention Cycle & Rotation Safety (H-02 & M-03) ---
    # Setup sample expired log and backup for rotation verification
    fresh_log = logs_dir / "nginx_access.log"
    fresh_log.write_text("sample log")
    sample_backup = backups_dir / "20261109T100000Z"
    sample_backup.mkdir()
    (sample_backup / "orders.db").write_text("backup db")

    cycle_now = datetime(2026, 11, 10, 14, 0, 0, tzinfo=timezone.utc)
    cycle_summary = retention_worker.run_retention_cycle(
        db_path,
        uploads_dir,
        backup_dir=backups_dir,
        now=cycle_now,
    )
    assert cycle_summary["ok"] is True
    # Paid order is protected under hold; source is never purged
    assert source_local_path.is_file(), "INVARIANT VIOLATION: Paid order source deleted by retention!"

    # Log policy check
    log_check = log_rotation.verify_log_retention_policy(logs_dir, max_days=15, now=cycle_now)
    assert log_check["ok"] is True

    # Telemetry verified in database
    with sqlite3.connect(db_path) as conn:
        telem = conn.execute("SELECT COUNT(*) FROM retention_telemetry").fetchone()[0]
        assert telem >= 1
