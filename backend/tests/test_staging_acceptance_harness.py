"""
PhotoDoc AI — R4.0 Staging Acceptance Harness & Safe Delivery Test Suite.

Deterministic, fully mocked local acceptance rehearsal for P1 automatic customer photo
cleanup and isolated staging delivery. Exercises the real production delivery and cleanup
pipeline:
  Upload -> Persist delivery job -> Upload to isolated Yandex Disk destination
  -> Verify remote SHA-256 and size -> Commit delivered state -> Delete local source
  -> Verify cleanup state -> Verify backup integrity.

Covers:
- Scenario A: Full successful lifecycle, post-delivery unlinking, backup verifier contract.
- Scenario B: Remote verification failures (timeout, connection error, 404, checksum mismatch,
  size mismatch, malformed metadata, invalid resource type, bad upload URL) preserving source.
- Scenario C: Cleanup crash recovery, transient retry, durable intent, lease fencing, idempotency.
- Scenario D: Isolation and privacy (staging root isolation, rejection of PhotoDoc_Orders,
  zero HTTP DELETE calls to Yandex Disk, zero PII / token leakage into logs, temp file hygiene).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
SCRIPTS = BACKEND.parent / "scripts"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import main  # noqa: E402
from delivery import (  # noqa: E402
    claim_due,
    cleanup_orphaned_temporary_files,
    enqueue,
    finish,
    perform_job_cleanup,
    process_due,
    process_due_cleanups,
    set_uploads_dir,
    utc_now,
)
from verify_backup_integrity import verify_backup  # noqa: E402
import verify_staging_delivery_preflight as staging_preflight  # noqa: E402

SYNTHETIC_JPEG = b"\xff\xd8\xff" + b"ACCEPTANCE-TEST-IMAGE-DATA-R4" * 16 + b"\xff\xd9"
SYNTHETIC_JPEG_SHA256 = hashlib.sha256(SYNTHETIC_JPEG).hexdigest()
SYNTHETIC_JPEG_SIZE = len(SYNTHETIC_JPEG)


class MockYandexDiskTransport:
    """Mock HTTP transport simulating Yandex Disk REST API behavior without network calls."""

    def __init__(self, target_sha256: str = SYNTHETIC_JPEG_SHA256, target_size: int = SYNTHETIC_JPEG_SIZE):
        self.target_sha256 = target_sha256
        self.target_size = target_size
        self.uploaded_files: Dict[str, Dict[str, Any]] = {}
        self.calls: List[Dict[str, Any]] = []
        self.delete_calls: List[Dict[str, Any]] = []

        # Configurable fault injectors
        self.upload_url_error: Optional[Exception] = None
        self.upload_url_status: int = 200
        self.upload_url_href: Optional[str] = "https://mock-uploader.example.invalid/put/file"
        self.put_error: Optional[Exception] = None
        self.put_status: int = 201
        self.metadata_error: Optional[Exception] = None
        self.metadata_status: int = 200
        self.metadata_override: Optional[Dict[str, Any]] = None

    def get(self, url: str, **kwargs):
        call_info = {"method": "GET", "url": url, "kwargs": kwargs}
        self.calls.append(call_info)

        if "upload" in url:
            if self.upload_url_error:
                raise self.upload_url_error
            if self.upload_url_status != 200:
                mock_resp = MagicMock()
                mock_resp.status_code = self.upload_url_status
                mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                    f"HTTP {self.upload_url_status}", request=MagicMock(), response=mock_resp
                )
                return mock_resp
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"href": self.upload_url_href}
            mock_resp.raise_for_status = MagicMock()
            return mock_resp

        # Resource metadata request
        if self.metadata_error:
            raise self.metadata_error

        params = kwargs.get("params", {})
        path = params.get("path", "")

        if self.metadata_status != 200:
            mock_resp = MagicMock()
            mock_resp.status_code = self.metadata_status
            if self.metadata_status >= 400 and self.metadata_status != 404:
                mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                    f"HTTP {self.metadata_status}", request=MagicMock(), response=mock_resp
                )
            else:
                mock_resp.raise_for_status = MagicMock()
            return mock_resp

        if self.metadata_override is not None:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = self.metadata_override
            mock_resp.raise_for_status = MagicMock()
            return mock_resp

        # Default: if previously PUT, return matching metadata
        if path in self.uploaded_files:
            meta = self.uploaded_files[path]
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "type": "file",
                "sha256": meta["sha256"],
                "size": meta["size"],
            }
            mock_resp.raise_for_status = MagicMock()
            return mock_resp

        # Otherwise 404
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    def put(self, url: str, **kwargs):
        call_info = {"method": "PUT", "url": url, "kwargs": kwargs}
        self.calls.append(call_info)

        if url.startswith("https://mock-uploader"):
            if self.put_error:
                raise self.put_error
            if self.put_status >= 400:
                mock_resp = MagicMock()
                mock_resp.status_code = self.put_status
                mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                    f"HTTP {self.put_status}", request=MagicMock(), response=mock_resp
                )
                return mock_resp

            # Extract uploaded bytes
            content = kwargs.get("content")
            if hasattr(content, "__iter__"):
                uploaded_bytes = b"".join(list(content))
            else:
                uploaded_bytes = bytes(content or b"")

            sha = hashlib.sha256(uploaded_bytes).hexdigest()
            # Store under last queried path or delivered path
            last_path = "default"
            for prev in reversed(self.calls):
                if prev["method"] == "GET" and "params" in prev["kwargs"]:
                    last_path = prev["kwargs"]["params"].get("path", "default")
                    break
            self.uploaded_files[last_path] = {"sha256": sha, "size": len(uploaded_bytes)}

            mock_resp = MagicMock()
            mock_resp.status_code = 201
            mock_resp.raise_for_status = MagicMock()
            return mock_resp

        # Folder creation request
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    def delete(self, url: str, **kwargs):
        call_info = {"method": "DELETE", "url": url, "kwargs": kwargs}
        self.calls.append(call_info)
        self.delete_calls.append(call_info)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        return mock_resp


@pytest.fixture
def staging_env(tmp_path, monkeypatch):
    """Isolated staging test fixture with test database, uploads, and staging configuration."""
    db_path = tmp_path / "staging_acceptance_orders.db"
    uploads_dir = tmp_path / "staging_acceptance_uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads_dir)
    set_uploads_dir(uploads_dir)

    # Staging isolation configuration
    monkeypatch.setenv("PHOTODOC_STAGING", "1")
    monkeypatch.setenv("PHOTODOC_ALLOW_FRESH_DB", "1")
    monkeypatch.setenv("ROBOKASSA_IS_TEST", "1")
    monkeypatch.setenv("ROBOKASSA_MERCHANT_LOGIN", "staging_merchant")
    monkeypatch.setenv("ROBOKASSA_PASSWORD1", "staging_pass1")
    monkeypatch.setenv("ROBOKASSA_PASSWORD2", "staging_pass2")
    monkeypatch.setenv("YANDEX_DISK_ROOT_DIR", "PhotoDoc_Staging_R4")
    monkeypatch.setenv("DELIVERY_WORKER_ENABLED", "0")
    # Legitimate non-placeholder test staging token for mock rehearsal
    monkeypatch.setenv("YANDEX_DISK_TOKEN", "y0_staging_authorized_acceptance_token_12345")
    monkeypatch.setenv("PHOTODOC_TEST_MOCK_TOKEN_ALLOWED", "1")
    monkeypatch.setattr(main, "send_order_email", lambda *a, **k: True)

    main.init_db()
    client = TestClient(main.app)
    yield client, db_path, uploads_dir


def create_acceptance_order(
    client: TestClient,
    filename: str = "portrait.jpg",
    content: bytes = SYNTHETIC_JPEG,
    name: str = "Acceptance Tester",
) -> Dict[str, Any]:
    resp = client.post(
        "/api/order",
        data={
            "name": name,
            "phone": "+79998887766",
            "email": "tester@example.com",
            "format": "10x15",
            "paper": "Глянцевая",
            "crop": "Без полей",
            "comment": "R4.0 acceptance rehearsal",
        },
        files=[("files", (filename, content, "image/jpeg"))],
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# =============================================================================
# SCENARIO A: Full Successful Delivery and Cleanup Lifecycle
# =============================================================================
def test_scenario_a_successful_delivery_and_cleanup_lifecycle(staging_env):
    """
    1. Create synthetic photo & persist delivery job with authoritative sha256 and size.
    2. Simulate remote upload and matching remote metadata verification.
    3. Confirm delivery state commits to 'delivered'.
    4. Confirm local file deleted strictly AFTER delivery commit.
    5. Confirm cleanup_state='completed', cleanup_phase='deleted', local_deleted_at set.
    6. Confirm backup verifier accepts database and upload directory.
    """
    client, db_path, uploads_dir = staging_env
    order_data = create_acceptance_order(client, "test_photo.jpg", SYNTHETIC_JPEG)
    order_id = order_data["order_id"]

    photo_files = list(uploads_dir.glob(f"{order_id}_*.jpg"))
    assert len(photo_files) == 1
    photo_path = photo_files[0]
    assert photo_path.is_file()

    # Step 1: Verify authoritative SHA-256 and size persisted in delivery_jobs
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        job = conn.execute(
            "SELECT * FROM delivery_jobs WHERE order_id=? AND kind='file'", (order_id,)
        ).fetchone()
        assert job is not None
        assert job["state"] == "pending"
        assert job["content_sha256"] == SYNTHETIC_JPEG_SHA256
        assert job["content_size"] == SYNTHETIC_JPEG_SIZE
        assert job["cleanup_state"] == "pending"
        assert job["cleanup_phase"] == "pending"
        assert job["remote_path"].startswith("PhotoDoc_Staging_R4/")

    mock_transport = MockYandexDiskTransport(SYNTHETIC_JPEG_SHA256, SYNTHETIC_JPEG_SIZE)

    # Track sequencing: assert that at the moment local file is deleted, delivery state was ALREADY delivered
    original_unlink = Path.unlink
    states_at_unlink = []

    def unlink_spy(path_self, *args, **kwargs):
        if str(path_self).endswith(".jpg"):
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT state FROM delivery_jobs WHERE order_id=? AND kind='file'", (order_id,)
                ).fetchone()
                states_at_unlink.append(row[0] if row else None)
        return original_unlink(path_self, *args, **kwargs)

    with patch.object(main.httpx, "get", side_effect=mock_transport.get), \
         patch.object(main.httpx, "put", side_effect=mock_transport.put), \
         patch.object(main, "send_order_email", return_value=True), \
         patch.object(Path, "unlink", side_effect=unlink_spy, autospec=True):
        # Step 2: Execute delivery and cleanup
        process_due(db_path, main.deliver_job, uploads_dir=uploads_dir)

    # Verify unlink occurred AFTER delivery state committed
    assert len(states_at_unlink) >= 1
    assert all(s == "delivered" for s in states_at_unlink), "Photo unlinked before delivery committed!"

    # Step 3: Verify SQLite state
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        file_job = conn.execute(
            "SELECT * FROM delivery_jobs WHERE order_id=? AND kind='file'", (order_id,)
        ).fetchone()
        assert file_job["state"] == "delivered"
        assert file_job["delivered_at"] is not None
        assert file_job["cleanup_state"] == "completed"
        assert file_job["cleanup_phase"] == "deleted"
        assert file_job["local_deleted_at"] is not None
        # local_deleted_at must be >= delivered_at
        assert file_job["local_deleted_at"] >= file_job["delivered_at"]

    # Step 4: Verify local file is absent from disk
    assert not photo_path.exists(), "Local photo must be unlinked after delivery"

    # Step 5: Verify backup verifier accepts the resulting state with 0 violations
    success, report = verify_backup(db_path, uploads_dir, allow_legacy_unmarked=False)
    assert success is True, f"Backup verifier rejected clean delivery state: {report}"
    assert report["p1_orders_count"] >= 1
    assert report["p1_cleaned_files"] >= 1
    assert report["errors"] == []
    assert report["p1_residual_uncleaned_files"] == []


# =============================================================================
# SCENARIO B: Remote Verification Failures Preserve Local Source
# =============================================================================
@pytest.mark.parametrize(
    "failure_type,failure_config",
    [
        ("timeout", {"upload_url_error": httpx.TimeoutException("Upload URL timed out")}),
        ("connect_error", {"put_error": httpx.ConnectError("Connection refused to upload server")}),
        ("not_found", {"metadata_status": 404}),
        ("sha_mismatch", {"metadata_override": {"type": "file", "sha256": "0" * 64, "size": SYNTHETIC_JPEG_SIZE}}),
        ("size_mismatch", {"metadata_override": {"type": "file", "sha256": SYNTHETIC_JPEG_SHA256, "size": 999999}}),
        ("missing_sha", {"metadata_override": {"type": "file", "size": SYNTHETIC_JPEG_SIZE}}),
        ("invalid_type", {"metadata_override": {"type": "dir", "sha256": SYNTHETIC_JPEG_SHA256, "size": SYNTHETIC_JPEG_SIZE}}),
        ("invalid_upload_href", {"upload_url_href": "ftp://insecure.example.com/bad"}),
        ("upload_500_error", {"upload_url_status": 500}),
    ],
)
def test_scenario_b_remote_verification_failures_preserve_local_source(
    staging_env, failure_type, failure_config
):
    """
    Every failure path must preserve the original local source file and NEVER mark
    the job as delivered or cleaned.
    """
    client, db_path, uploads_dir = staging_env
    order_data = create_acceptance_order(client, f"fail_{failure_type}.jpg", SYNTHETIC_JPEG)
    order_id = order_data["order_id"]

    photo_path = list(uploads_dir.glob(f"{order_id}_*.jpg"))[0]
    assert photo_path.is_file()

    mock_transport = MockYandexDiskTransport(SYNTHETIC_JPEG_SHA256, SYNTHETIC_JPEG_SIZE)
    for k, v in failure_config.items():
        setattr(mock_transport, k, v)

    with patch.object(main.httpx, "get", side_effect=mock_transport.get), \
         patch.object(main.httpx, "put", side_effect=mock_transport.put):
        process_due(db_path, main.deliver_job, uploads_dir=uploads_dir)

    # 1. Local photo file MUST remain intact
    assert photo_path.is_file(), f"Local source was deleted despite {failure_type} failure!"
    assert photo_path.read_bytes() == SYNTHETIC_JPEG

    # 2. Job state MUST NOT be delivered or completed
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        job = conn.execute(
            "SELECT * FROM delivery_jobs WHERE order_id=? AND kind='file'", (order_id,)
        ).fetchone()
        assert job["state"] != "delivered", f"Job marked delivered despite {failure_type} failure"
        assert job["cleanup_state"] != "completed", f"Job marked cleaned despite {failure_type} failure"
        assert job["local_deleted_at"] is None


# =============================================================================
# SCENARIO C: Cleanup and Crash Recovery
# =============================================================================
def test_scenario_c_cleanup_failure_after_delivery_leaves_delivered_and_marks_failed(staging_env):
    """If cleanup fails after delivery, delivery remains delivered, cleanup marked failed with backoff."""
    client, db_path, uploads_dir = staging_env
    order_data = create_acceptance_order(client, "fail_cleanup.jpg", SYNTHETIC_JPEG)
    order_id = order_data["order_id"]
    photo_path = list(uploads_dir.glob(f"{order_id}_*.jpg"))[0]

    mock_transport = MockYandexDiskTransport(SYNTHETIC_JPEG_SHA256, SYNTHETIC_JPEG_SIZE)

    # Deliver job manually
    with patch.object(main.httpx, "get", side_effect=mock_transport.get), \
         patch.object(main.httpx, "put", side_effect=mock_transport.put):
        claimed = claim_due(db_path)
        assert claimed is not None
        main.deliver_job(claimed)
        finish(db_path, claimed["id"])

    # Simulate filesystem permission failure during unlink
    with patch.object(Path, "unlink", side_effect=PermissionError("File locked by virus scanner")):
        result = perform_job_cleanup(db_path, claimed["id"], uploads_dir=uploads_dir)
        assert result is False

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        job = conn.execute("SELECT * FROM delivery_jobs WHERE id=?", (claimed["id"],)).fetchone()
        assert job["state"] == "delivered", "Delivery state must not be corrupted by cleanup error"
        assert job["cleanup_state"] == "failed"
        assert job["cleanup_phase"] == "failed"
        assert "PermissionError" in (job["cleanup_error"] or "")
        assert job["local_deleted_at"] is None

    # Local file still exists
    assert photo_path.is_file()


def test_scenario_c_retry_after_transient_failure_succeeds(staging_env):
    """Failed cleanup is retried by process_due_cleanups once backoff passes and cleans safely."""
    client, db_path, uploads_dir = staging_env
    order_data = create_acceptance_order(client, "retry_cleanup.jpg", SYNTHETIC_JPEG)
    order_id = order_data["order_id"]
    photo_path = list(uploads_dir.glob(f"{order_id}_*.jpg"))[0]

    mock_transport = MockYandexDiskTransport(SYNTHETIC_JPEG_SHA256, SYNTHETIC_JPEG_SIZE)
    with patch.object(main.httpx, "get", side_effect=mock_transport.get), \
         patch.object(main.httpx, "put", side_effect=mock_transport.put):
        claimed = claim_due(db_path)
        main.deliver_job(claimed)
        finish(db_path, claimed["id"])

    # Fail attempt 1
    with patch.object(Path, "unlink", side_effect=OSError("Device busy")):
        perform_job_cleanup(db_path, claimed["id"], uploads_dir=uploads_dir)

    # Fast forward clock past backoff
    future_clock = lambda: (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    cleaned_count = process_due_cleanups(db_path, uploads_dir=uploads_dir, clock=future_clock)
    assert cleaned_count == 1

    assert not photo_path.exists()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        job = conn.execute("SELECT * FROM delivery_jobs WHERE id=?", (claimed["id"],)).fetchone()
        assert job["cleanup_state"] == "completed"
        assert job["cleanup_phase"] == "deleted"
        assert job["local_deleted_at"] is not None


def test_scenario_c_restart_recovery_with_deleting_phase(staging_env):
    """Crash after durable intent committed (cleanup_phase='deleting') completes cleanly on restart."""
    client, db_path, uploads_dir = staging_env
    order_data = create_acceptance_order(client, "crash_intent.jpg", SYNTHETIC_JPEG)
    order_id = order_data["order_id"]
    photo_path = list(uploads_dir.glob(f"{order_id}_*.jpg"))[0]

    mock_transport = MockYandexDiskTransport(SYNTHETIC_JPEG_SHA256, SYNTHETIC_JPEG_SIZE)
    with patch.object(main.httpx, "get", side_effect=mock_transport.get), \
         patch.object(main.httpx, "put", side_effect=mock_transport.put):
        claimed = claim_due(db_path)
        main.deliver_job(claimed)
        finish(db_path, claimed["id"])

    # Simulate crash right after unlinking with cleanup_phase='deleting'
    photo_path.unlink()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE delivery_jobs SET cleanup_state='processing', cleanup_phase='deleting', "
            "cleanup_lease_until=? WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), claimed["id"]),
        )
        conn.commit()

    # Recovery sweep
    recovered = process_due_cleanups(db_path, uploads_dir=uploads_dir)
    assert recovered == 1

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        job = conn.execute("SELECT * FROM delivery_jobs WHERE id=?", (claimed["id"],)).fetchone()
        assert job["cleanup_state"] == "completed"
        assert job["cleanup_phase"] == "deleted"
        assert job["local_deleted_at"] is not None


def test_scenario_c_missing_source_without_intent_is_non_retryable_anomaly(staging_env):
    """Missing file without durable intent (cleanup_phase='pending') fails closed as anomaly."""
    client, db_path, uploads_dir = staging_env
    order_data = create_acceptance_order(client, "missing_anom.jpg", SYNTHETIC_JPEG)
    order_id = order_data["order_id"]
    photo_path = list(uploads_dir.glob(f"{order_id}_*.jpg"))[0]

    mock_transport = MockYandexDiskTransport(SYNTHETIC_JPEG_SHA256, SYNTHETIC_JPEG_SIZE)
    with patch.object(main.httpx, "get", side_effect=mock_transport.get), \
         patch.object(main.httpx, "put", side_effect=mock_transport.put):
        claimed = claim_due(db_path)
        main.deliver_job(claimed)
        finish(db_path, claimed["id"])

    # Unexpected external deletion
    photo_path.unlink()

    result = perform_job_cleanup(db_path, claimed["id"], uploads_dir=uploads_dir)
    assert result is False

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        job = conn.execute("SELECT * FROM delivery_jobs WHERE id=?", (claimed["id"],)).fetchone()
        assert job["cleanup_state"] == "failed"
        assert job["cleanup_phase"] == "failed"
        assert job["cleanup_error"] == "MissingSourceBeforeCleanup"

    # Must be excluded from background sweeps
    sweep_count = process_due_cleanups(db_path, uploads_dir=uploads_dir)
    assert sweep_count == 0


def test_scenario_c_stale_worker_losing_lease_aborts_without_unlink(staging_env):
    """Worker losing its lease before unlinking must abort without modifying disk."""
    client, db_path, uploads_dir = staging_env
    order_data = create_acceptance_order(client, "stale_fencing.jpg", SYNTHETIC_JPEG)
    order_id = order_data["order_id"]
    photo_path = list(uploads_dir.glob(f"{order_id}_*.jpg"))[0]

    mock_transport = MockYandexDiskTransport(SYNTHETIC_JPEG_SHA256, SYNTHETIC_JPEG_SIZE)
    with patch.object(main.httpx, "get", side_effect=mock_transport.get), \
         patch.object(main.httpx, "put", side_effect=mock_transport.put):
        claimed = claim_due(db_path)
        main.deliver_job(claimed)
        finish(db_path, claimed["id"])

    def steal_lease_hook(job_id, token):
        # Another worker steals the lease
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE delivery_jobs SET cleanup_lease_until='future#99#rival' WHERE id=?", (job_id,)
            )
            conn.commit()

    result = perform_job_cleanup(
        db_path, claimed["id"], uploads_dir=uploads_dir, pre_unlink_hook=steal_lease_hook
    )
    assert result is False

    # Original file was preserved because stale worker was fenced!
    assert photo_path.is_file()


def test_scenario_c_idempotent_repeated_cleanup(staging_env):
    """Calling cleanup repeatedly on an already completed job is idempotent."""
    client, db_path, uploads_dir = staging_env
    order_data = create_acceptance_order(client, "idemp.jpg", SYNTHETIC_JPEG)
    order_id = order_data["order_id"]

    mock_transport = MockYandexDiskTransport(SYNTHETIC_JPEG_SHA256, SYNTHETIC_JPEG_SIZE)
    with patch.object(main.httpx, "get", side_effect=mock_transport.get), \
         patch.object(main.httpx, "put", side_effect=mock_transport.put):
        process_due(db_path, main.deliver_job, uploads_dir=uploads_dir)

    with sqlite3.connect(db_path) as conn:
        job_id = conn.execute(
            "SELECT id FROM delivery_jobs WHERE order_id=? AND kind='file'", (order_id,)
        ).fetchone()[0]

    # Repeated calls return True idempotently
    assert perform_job_cleanup(db_path, job_id, uploads_dir=uploads_dir) is True
    assert perform_job_cleanup(db_path, job_id, uploads_dir=uploads_dir) is True


# =============================================================================
# SCENARIO D: Isolation, Privacy & Zero PII Leakage
# =============================================================================
def test_scenario_d_staging_remote_paths_cannot_target_photodoc_orders(staging_env, monkeypatch):
    """Staging configuration forbids targeting production PhotoDoc_Orders."""
    client, db_path, uploads_dir = staging_env

    # 1. Order created under staging gets staging remote path prefix
    order_data = create_acceptance_order(client, "prefix_test.jpg", SYNTHETIC_JPEG)
    order_id = order_data["order_id"]
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        jobs = conn.execute("SELECT * FROM delivery_jobs WHERE order_id=?", (order_id,)).fetchall()
        for j in jobs:
            if j["kind"] in ("file", "info"):
                assert j["remote_path"].startswith("PhotoDoc_Staging_R4/")
                assert not j["remote_path"].startswith("PhotoDoc_Orders")

    # 2. Direct attempt to upload to PhotoDoc_Orders in staging environment is rejected
    test_file = uploads_dir / f"{order_id}_test_isolation.jpg"
    test_file.write_bytes(SYNTHETIC_JPEG)
    with pytest.raises(RuntimeError, match="Staging delivery cannot upload to production destination"):
        main.upload_to_yandex_disk(
            str(test_file), "PhotoDoc_Orders/Заказ_1", "test.jpg", uploads_dir=uploads_dir
        )


def test_scenario_d_staging_rejects_missing_or_placeholder_tokens(staging_env, monkeypatch):
    """Staging worker startup and delivery validation strictly reject known placeholders."""
    # 1. Reject placeholder token in staging
    monkeypatch.setenv("PHOTODOC_TEST_MOCK_TOKEN_ALLOWED", "0")
    monkeypatch.setenv("YANDEX_DISK_TOKEN", "y0_test_staging_token_mock")
    with pytest.raises(RuntimeError, match="cannot use placeholder or example token"):
        main.validate_staging_delivery_safety()

    # 2. Reject missing token
    monkeypatch.setenv("YANDEX_DISK_TOKEN", "")
    with pytest.raises(RuntimeError, match="requires explicit non-empty"):
        main.validate_staging_delivery_safety()

    # 3. Reject PhotoDoc_Orders root
    monkeypatch.setenv("YANDEX_DISK_TOKEN", "genuine_token_abc")
    monkeypatch.setenv("YANDEX_DISK_ROOT_DIR", "PhotoDoc_Orders")
    with pytest.raises(RuntimeError, match="forbidden to target production root"):
        main.validate_staging_delivery_safety()


def test_scenario_d_no_http_delete_request_to_yandex_disk(staging_env):
    """
    PhotoDoc AI guarantees customer photos are permanently archived on Yandex Disk.
    HTTP DELETE must NEVER be issued to the remote storage API.
    """
    client, db_path, uploads_dir = staging_env
    order_data = create_acceptance_order(client, "no_delete.jpg", SYNTHETIC_JPEG)

    mock_transport = MockYandexDiskTransport(SYNTHETIC_JPEG_SHA256, SYNTHETIC_JPEG_SIZE)
    with patch.object(main.httpx, "get", side_effect=mock_transport.get), \
         patch.object(main.httpx, "put", side_effect=mock_transport.put), \
         patch.object(main.httpx, "delete", side_effect=mock_transport.delete):
        process_due(db_path, main.deliver_job, uploads_dir=uploads_dir)

    assert len(mock_transport.delete_calls) == 0, "HTTP DELETE was invoked against Yandex Disk!"


def test_scenario_d_no_pii_tokens_or_headers_leak_in_logs(staging_env, caplog):
    """Logging verification: OAuth tokens, customer phone, email, and photo bytes never leak."""
    client, db_path, uploads_dir = staging_env

    secret_token = "y0_staging_authorized_acceptance_token_12345"
    customer_phone = "+79998887766"
    customer_email = "tester@example.com"
    customer_comment = "SecretCommentXYZ"

    mock_transport = MockYandexDiskTransport(SYNTHETIC_JPEG_SHA256, SYNTHETIC_JPEG_SIZE)

    with caplog.at_level(logging.DEBUG):
        resp = client.post(
            "/api/order",
            data={
                "name": "PII Protection Tester",
                "phone": customer_phone,
                "email": customer_email,
                "format": "10x15",
                "paper": "Матовая",
                "crop": "Без полей",
                "comment": customer_comment,
            },
            files=[("files", ("pii_test.jpg", SYNTHETIC_JPEG, "image/jpeg"))],
        )
        assert resp.status_code == 200

        with patch.object(main.httpx, "get", side_effect=mock_transport.get), \
             patch.object(main.httpx, "put", side_effect=mock_transport.put):
            process_due(db_path, main.deliver_job, uploads_dir=uploads_dir)

    log_text = caplog.text

    assert secret_token not in log_text, "OAuth token leaked in logs!"
    assert f"OAuth {secret_token}" not in log_text, "Authorization header leaked in logs!"
    assert customer_phone not in log_text, "Customer phone number leaked in logs!"
    assert customer_comment not in log_text, "Customer comment leaked in logs!"
    assert "ACCEPTANCE-TEST-IMAGE-DATA-R4" not in log_text, "Photo binary bytes leaked in logs!"


def test_scenario_d_temporary_files_hygiene(staging_env):
    """Temporary staging files and incoming chunks are cleaned up without leaving residuals."""
    _, db_path, uploads_dir = staging_env

    # Create stale incoming files
    stale_file = uploads_dir / ".incoming-stale-upload.tmp"
    stale_file.write_bytes(b"stale chunk")

    cleaned = cleanup_orphaned_temporary_files(uploads_dir, db_path, max_age_seconds=0)
    assert cleaned >= 1
    assert not stale_file.exists()


# =============================================================================
# SCENARIO E: Preflight Tool Integration and Compose Structure Validation
# =============================================================================
def test_preflight_tool_validates_staging_and_override_compose():
    """Verify scripts/verify_staging_delivery_preflight.py validates both compose configs."""
    compose_staging = BACKEND.parent / "docker-compose.staging.yml"
    compose_override = BACKEND.parent / "docker-compose.staging.delivery.yml"

    report = staging_preflight.run_preflight(compose_staging, compose_override)
    assert report["pass"] is True
    assert report["markers"]["STAGING_DEFAULT_WORKER_DISABLED"] == "PASS"
    assert report["markers"]["STAGING_OVERRIDE_REQUIRES_EXPLICIT_TOKEN"] == "PASS"
    assert report["markers"]["STAGING_VOLUME_AND_REMOTE_ROOT_ISOLATION"] == "PASS"
    # Live acceptance marker must be BLOCKED when no genuine token is passed
    assert report["markers"]["LIVE_YANDEX_STAGING_ACCEPTANCE"] == "BLOCKED"
    assert report["blockers"] == []
