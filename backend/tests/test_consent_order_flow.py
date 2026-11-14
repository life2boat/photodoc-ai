"""Comprehensive tests for H-05: Consent enforcement in order-creation path."""
import hashlib
import io
import os
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import consents
import main
from main import app, DB_PATH, UPLOAD_DIR


@pytest.fixture(autouse=True)
def setup_isolated_env(tmp_path, monkeypatch):
    test_db = tmp_path / "test_orders.db"
    test_uploads = tmp_path / "uploads"
    test_uploads.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(main, "DB_PATH", test_db)
    monkeypatch.setattr(main, "UPLOAD_DIR", test_uploads)
    main.set_uploads_dir(test_uploads)
    main.init_db()

    # Disable delivery worker in lifespan for isolated tests
    monkeypatch.setenv("DELIVERY_WORKER_ENABLED", "0")
    monkeypatch.setenv("RETENTION_WORKER_ENABLED", "0")

    yield {"db": test_db, "uploads": test_uploads}


SOURCE_BYTES = b"\xff\xd8\xff\xe0" + b"REAL-SOURCE-ORIGINAL-" * 8


def _dummy_file():
    return ("source.jpg", io.BytesIO(SOURCE_BYTES), "image/jpeg")


def test_order_rejected_when_flag_absent_and_no_consent(monkeypatch, setup_isolated_env):
    """Test A: flag absent + no consent -> rejected (fail-closed)."""
    monkeypatch.delenv(consents.FEATURE_FLAG, raising=False)
    client = TestClient(app)

    response = client.post(
        "/api/order",
        data={
            "name": "Иван Иванов",
            "phone": "+79991234567",
            "format": "DOC_3X4",
            "paper": "Цифровая обработка",
            "crop": "По стандарту документа",
        },
        files={"files": _dummy_file()},
    )
    assert response.status_code == 400
    assert "consent enforcement system is not configured" in response.json()["detail"]


def test_order_rejected_when_flag_disabled_and_no_consent(monkeypatch, setup_isolated_env):
    """Test B: flag disabled ('0') + no consent -> rejected (fail-closed)."""
    monkeypatch.setenv(consents.FEATURE_FLAG, "0")
    client = TestClient(app)

    response = client.post(
        "/api/order",
        data={
            "name": "Иван Иванов",
            "phone": "+79991234567",
            "format": "DOC_3X4",
            "paper": "Цифровая обработка",
            "crop": "По стандарту документа",
        },
        files={"files": _dummy_file()},
    )
    assert response.status_code == 400
    assert "consent enforcement system is not configured" in response.json()["detail"]


def test_order_rejected_when_flag_absent_and_supplied_consent(monkeypatch, setup_isolated_env):
    """Test C: flag absent + supplied consent -> rejected (fail-closed)."""
    monkeypatch.delenv(consents.FEATURE_FLAG, raising=False)
    client = TestClient(app)

    response = client.post(
        "/api/order",
        data={
            "name": "Иван Иванов",
            "phone": "+79991234567",
            "format": "DOC_3X4",
            "paper": "Цифровая обработка",
            "crop": "По стандарту документа",
            "consent_service_terms": "true",
            "consent_personal_data": "true",
        },
        files={"files": _dummy_file()},
    )
    assert response.status_code == 400
    assert "consent enforcement system is not configured" in response.json()["detail"]


def test_order_rejected_when_consents_missing_and_feature_enabled(monkeypatch):
    monkeypatch.setenv(consents.FEATURE_FLAG, "1")
    client = TestClient(app)

    # Missing all consents
    response = client.post(
        "/api/order",
        data={
            "name": "Пётр Петров",
            "phone": "+79997654321",
            "format": "3x4",
            "paper": "матовая",
            "crop": "стандартная",
        },
        files={"files": _dummy_file()},
    )
    assert response.status_code == 400
    assert "Mandatory consents" in response.json()["detail"]


def test_order_rejected_when_only_one_mandatory_consent_provided(monkeypatch):
    monkeypatch.setenv(consents.FEATURE_FLAG, "1")
    client = TestClient(app)

    # Only service terms
    r1 = client.post(
        "/api/order",
        data={
            "name": "Пётр",
            "phone": "+79991112233",
            "consent_service_terms": "true",
            "consent_personal_data": "false",
        },
        files={"files": _dummy_file()},
    )
    assert r1.status_code == 400
    assert "Mandatory consents" in r1.json()["detail"]

    # Only personal data
    r2 = client.post(
        "/api/order",
        data={
            "name": "Пётр",
            "phone": "+79991112233",
            "consent_service_terms": "false",
            "consent_personal_data": "true",
        },
        files={"files": _dummy_file()},
    )
    assert r2.status_code == 400
    assert "Mandatory consents" in r2.json()["detail"]


def test_order_rejected_when_mandatory_documents_unapproved(monkeypatch, setup_isolated_env):
    """When mandatory documents lack legal approval, order creation is rejected fail-closed."""
    monkeypatch.setenv(consents.FEATURE_FLAG, "1")
    # Both mandatory documents remain unapproved (default state)
    assert consents.DOCUMENTS[consents.PURPOSE_SERVICE]["approved"] is False
    assert consents.DOCUMENTS[consents.PURPOSE_PERSONAL_DATA]["approved"] is False

    client = TestClient(app)
    response = client.post(
        "/api/order",
        data={
            "name": "Иван",
            "phone": "+79991234567",
            "format": "DOC_3X4",
            "paper": "Цифровая обработка",
            "crop": "По стандарту документа",
            "consent_service_terms": "true",
            "consent_personal_data": "true",
            "consent_doc_version": consents.CURRENT_DRAFT_VERSION,
            "consent_doc_hash": consents.CANONICAL_OFFER_HASH,
        },
        files={"files": _dummy_file()},
    )
    assert response.status_code == 400
    assert "blocked pending legal approval" in response.json()["detail"]

    # Verify fail-closed invariants: 0 orders, 0 consents, 0 files
    with sqlite3.connect(setup_isolated_env["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM consent_records").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM delivery_jobs").fetchone()[0] == 0
    assert list(setup_isolated_env["uploads"].iterdir()) == []


def test_order_rejected_when_service_terms_unapproved_alone(monkeypatch):
    monkeypatch.setenv(consents.FEATURE_FLAG, "1")
    monkeypatch.setitem(consents.DOCUMENTS[consents.PURPOSE_SERVICE], "approved", False)
    monkeypatch.setitem(consents.DOCUMENTS[consents.PURPOSE_PERSONAL_DATA], "approved", True)

    client = TestClient(app)
    response = client.post(
        "/api/order",
        data={
            "name": "Иван",
            "phone": "+79991234567",
            "consent_service_terms": "true",
            "consent_personal_data": "true",
        },
        files={"files": _dummy_file()},
    )
    assert response.status_code == 400
    assert f"Consent for {consents.PURPOSE_SERVICE} is blocked pending legal approval" in response.json()["detail"]


def test_order_rejected_when_personal_data_unapproved_alone(monkeypatch):
    monkeypatch.setenv(consents.FEATURE_FLAG, "1")
    monkeypatch.setitem(consents.DOCUMENTS[consents.PURPOSE_SERVICE], "approved", True)
    monkeypatch.setitem(consents.DOCUMENTS[consents.PURPOSE_PERSONAL_DATA], "approved", False)

    client = TestClient(app)
    response = client.post(
        "/api/order",
        data={
            "name": "Иван",
            "phone": "+79991234567",
            "consent_service_terms": "true",
            "consent_personal_data": "true",
        },
        files={"files": _dummy_file()},
    )
    assert response.status_code == 400
    assert f"Consent for {consents.PURPOSE_PERSONAL_DATA} is blocked pending legal approval" in response.json()["detail"]


def test_order_rejected_on_doc_hash_mismatch(monkeypatch, setup_isolated_env):
    monkeypatch.setenv(consents.FEATURE_FLAG, "1")
    monkeypatch.setitem(consents.DOCUMENTS[consents.PURPOSE_SERVICE], "approved", True)
    monkeypatch.setitem(consents.DOCUMENTS[consents.PURPOSE_PERSONAL_DATA], "approved", True)
    client = TestClient(app)

    response = client.post(
        "/api/order",
        data={
            "name": "Анна",
            "phone": "+79993334455",
            "consent_service_terms": "true",
            "consent_personal_data": "true",
            "consent_doc_hash": "deadbeef" * 8,
        },
        files={"files": _dummy_file()},
    )
    assert response.status_code == 400
    assert "Invalid consent document hash" in response.json()["detail"]

    # Verify atomic rollback: 0 orders, 0 files in upload directory
    with sqlite3.connect(setup_isolated_env["db"]) as conn:
        order_count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        assert order_count == 0

    assert list(setup_isolated_env["uploads"].iterdir()) == []


def test_order_rejected_on_doc_version_mismatch(monkeypatch):
    monkeypatch.setenv(consents.FEATURE_FLAG, "1")
    monkeypatch.setitem(consents.DOCUMENTS[consents.PURPOSE_SERVICE], "approved", True)
    monkeypatch.setitem(consents.DOCUMENTS[consents.PURPOSE_PERSONAL_DATA], "approved", True)
    client = TestClient(app)

    response = client.post(
        "/api/order",
        data={
            "name": "Анна",
            "phone": "+79993334455",
            "consent_service_terms": "true",
            "consent_personal_data": "true",
            "consent_doc_version": "1999-01-01",
        },
        files={"files": _dummy_file()},
    )
    assert response.status_code == 400
    assert "Invalid consent document version" in response.json()["detail"]


def test_order_rejected_for_unapproved_conditional_purposes(monkeypatch):
    monkeypatch.setenv(consents.FEATURE_FLAG, "1")
    monkeypatch.setitem(consents.DOCUMENTS[consents.PURPOSE_SERVICE], "approved", True)
    monkeypatch.setitem(consents.DOCUMENTS[consents.PURPOSE_PERSONAL_DATA], "approved", True)
    client = TestClient(app)

    response = client.post(
        "/api/order",
        data={
            "name": "Анна",
            "phone": "+79993334455",
            "consent_service_terms": "true",
            "consent_personal_data": "true",
            "consent_third_party_images": "true",
        },
        files={"files": _dummy_file()},
    )
    assert response.status_code == 400
    assert "blocked pending legal approval" in response.json()["detail"]


def test_order_succeeds_with_valid_consents_and_evidence_query(monkeypatch, setup_isolated_env):
    monkeypatch.setenv(consents.FEATURE_FLAG, "1")
    monkeypatch.setitem(consents.DOCUMENTS[consents.PURPOSE_SERVICE], "approved", True)
    monkeypatch.setitem(consents.DOCUMENTS[consents.PURPOSE_PERSONAL_DATA], "approved", True)
    client = TestClient(app)

    canonical_offer_hash = consents.CANONICAL_OFFER_HASH
    response = client.post(
        "/api/order",
        data={
            "name": "Сергей",
            "phone": "+79995556677",
            "format": "DOC_3X4",
            "paper": "Цифровая обработка",
            "crop": "По стандарту документа",
            "consent_service_terms": "true",
            "consent_personal_data": "true",
            "consent_doc_version": consents.CURRENT_DRAFT_VERSION,
            "consent_doc_hash": canonical_offer_hash,
        },
        files={"files": _dummy_file()},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    order_id = data["order_id"]
    order_token = data["order_token"]

    # Query consent evidence with valid token
    ev_resp = client.get(f"/api/order/{order_id}/consents?order_token={order_token}")
    assert ev_resp.status_code == 200
    evidence = ev_resp.json()["consents"]
    purposes = {e["purpose"]: e for e in evidence}

    assert consents.PURPOSE_SERVICE in purposes
    assert consents.PURPOSE_PERSONAL_DATA in purposes
    assert purposes[consents.PURPOSE_SERVICE]["document_hash"] == canonical_offer_hash
    assert purposes[consents.PURPOSE_SERVICE]["action"] == "granted"

    # Query with invalid token
    bad_resp = client.get(f"/api/order/{order_id}/consents?order_token=invalid_token")
    assert bad_resp.status_code == 403


def test_order_with_consents_json_payload(monkeypatch):
    monkeypatch.setenv(consents.FEATURE_FLAG, "1")
    monkeypatch.setitem(consents.DOCUMENTS[consents.PURPOSE_SERVICE], "approved", True)
    monkeypatch.setitem(consents.DOCUMENTS[consents.PURPOSE_PERSONAL_DATA], "approved", True)
    client = TestClient(app)

    import json
    consents_payload = json.dumps({
        "service_terms": True,
        "personal_data_processing": True,
        "doc_version": consents.CURRENT_DRAFT_VERSION,
        "doc_hash": consents.CANONICAL_OFFER_HASH,
    })

    response = client.post(
        "/api/order",
        data={
            "name": "Ольга",
            "phone": "+79998889900",
            "format": "DOC_3X4",
            "paper": "Цифровая обработка",
            "crop": "По стандарту документа",
            "consents_json": consents_payload,
        },
        files={"files": _dummy_file()},
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True


def test_failed_consent_validation_leaves_zero_orders_and_zero_files(monkeypatch, setup_isolated_env):
    """Test G: failed consent validation leaves zero orders and zero orphaned customer files."""
    monkeypatch.setenv(consents.FEATURE_FLAG, "1")
    monkeypatch.setitem(consents.DOCUMENTS[consents.PURPOSE_SERVICE], "approved", True)
    monkeypatch.setitem(consents.DOCUMENTS[consents.PURPOSE_PERSONAL_DATA], "approved", True)
    client = TestClient(app)

    # 1. Attempt with invalid consent hash
    resp_bad_hash = client.post(
        "/api/order",
        data={
            "name": "Test Customer",
            "phone": "+79990001122",
            "format": "DOC_3X4",
            "paper": "Цифровая обработка",
            "crop": "По стандарту документа",
            "consent_service_terms": "true",
            "consent_personal_data": "true",
            "consent_doc_hash": "0000000000000000000000000000000000000000000000000000000000000000",
        },
        files={"files": _dummy_file()},
    )
    assert resp_bad_hash.status_code == 400

    # 2. Attempt with unapproved purpose
    resp_unapproved = client.post(
        "/api/order",
        data={
            "name": "Test Customer 2",
            "phone": "+79990001133",
            "format": "DOC_3X4",
            "paper": "Цифровая обработка",
            "crop": "По стандарту документа",
            "consent_service_terms": "true",
            "consent_personal_data": "true",
            "consent_third_party_images": "true",
        },
        files={"files": _dummy_file()},
    )
    assert resp_unapproved.status_code == 400

    # 3. Verify absolute database and filesystem cleanliness:
    # 0 orders, 0 consent_records, 0 delivery_jobs, 0 files in uploads directory
    with sqlite3.connect(setup_isolated_env["db"]) as conn:
        orders_count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        assert orders_count == 0, f"Expected 0 orders, found {orders_count}"

        consents_count = conn.execute("SELECT COUNT(*) FROM consent_records").fetchone()[0]
        assert consents_count == 0, f"Expected 0 consent_records, found {consents_count}"

        jobs_count = conn.execute("SELECT COUNT(*) FROM delivery_jobs").fetchone()[0]
        assert jobs_count == 0, f"Expected 0 delivery_jobs, found {jobs_count}"

    uploads_files = list(setup_isolated_env["uploads"].iterdir())
    assert uploads_files == [], f"Expected 0 files in uploads, found {uploads_files}"


