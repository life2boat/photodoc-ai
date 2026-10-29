"""
PhotoDoc AI — Privacy by Design & Automated Photo Cleanup Test Suite.

Validates all 15 regression scenarios required by P1 photo cleanup:
1. Successful delivery of a single photo -> local original unlinked from uploads.
2. Multiple photos -> each unlinked independently upon its own delivery.
3. Yandex.Disk error -> local photo preserved on disk.
4. SHA-256 mismatch -> deletion rejected.
5. SQLite commit failure / lost lease -> local photo preserved on disk.
6. Cleanup failure (e.g. file lock) -> retried later; delivery state remains 'delivered'.
7. Restart after delivery before cleanup -> cleanup recovered and finished.
8. Repeated processing -> idempotent, no re-upload, no corrupt state.
9. Expired lease -> stale worker cannot perform deletion.
10. Attempted deletion outside uploads -> rejected with PathOutsideUploads.
11. Symlink or file substitution -> rejected with SymlinkDetected.
12. info.txt unlinked after confirmed delivery.
13. Backup verifier accepts cleanly delivered and cleaned orders.
14. Backup verifier rejects loss of undelivered or uncleaned files.
15. Payments, emails, and order queries work seamlessly with cleaned files.
16. Stale temporary (.incoming-*) files cleaned safely while order files preserved.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

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

JPEG = b"\xff\xd8\xffsynthetic-photo-data-for-testing"  # 35 bytes
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRsynthetic-png-bytes"


class MockYandexResponse:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"Synthetic Yandex Disk HTTP {self.status_code}")


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    db = tmp_path / "orders.db"
    uploads = tmp_path / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(main, "DB_PATH", db)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads)
    set_uploads_dir(uploads)

    monkeypatch.setenv("ROBOKASSA_MERCHANT_LOGIN", "test-merchant")
    monkeypatch.setenv("ROBOKASSA_PASSWORD1", "test-pass-1")
    monkeypatch.setenv("ROBOKASSA_PASSWORD2", "test-pass-2")
    monkeypatch.setenv("DELIVERY_WORKER_ENABLED", "0")
    monkeypatch.setenv("YANDEX_DISK_TOKEN", "test-oauth-token")

    main.init_db()
    client = TestClient(main.app)
    yield client, db, uploads


def create_test_order(client, filenames=("photo1.jpg",), content=JPEG):
    files = [("files", (fn, content, "image/jpeg")) for fn in filenames]
    resp = client.post(
        "/api/order",
        data={
            "name": "Test Client",
            "phone": "+79991234567",
            "email": "client@example.com",
            "format": "10x15",
            "paper": "Матовая",
            "crop": "Без полей",
            "comment": "Privacy test order",
        },
        files=files,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# -----------------------------------------------------------------------------
# Scenario 1: Successful delivery of single file -> local file unlinked
# -----------------------------------------------------------------------------
def test_successful_delivery_single_file_unlinks_local_file(isolated_env):
    client, db, uploads = isolated_env
    order_data = create_test_order(client, ("vacation.jpg",))
    order_id = order_data["order_id"]

    photo_files_before = list(uploads.glob(f"{order_id}_*.jpg"))
    assert len(photo_files_before) == 1
    photo_path = photo_files_before[0]
    assert photo_path.is_file()

    expected_sha = hashlib.sha256(JPEG).hexdigest()
    expected_size = len(JPEG)

    uploaded_files = {}

    def fake_get(url, **kwargs):
        if url.endswith("/upload"):
            return MockYandexResponse(data={"href": "https://upload.example.invalid/put"})
        rpath = kwargs.get("params", {}).get("path", "")
        if rpath in uploaded_files:
            return MockYandexResponse(data={"type": "file", "sha256": uploaded_files[rpath]["sha256"], "size": uploaded_files[rpath]["size"]})
        return MockYandexResponse(status=404)

    def fake_put(url, **kwargs):
        if url.startswith("https://upload."):
            uploaded_files.setdefault("delivered", {"sha256": expected_sha, "size": expected_size})
            return MockYandexResponse(status=201)
        return MockYandexResponse(status=201)

    with patch.object(main.httpx, "get", side_effect=fake_get), \
         patch.object(main.httpx, "put", side_effect=fake_put), \
         patch.object(main, "_remote_verified", return_value=True):
        process_due(db, main.deliver_job, uploads_dir=uploads)

    # Verify SQLite state
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        job = conn.execute("SELECT * FROM delivery_jobs WHERE order_id=? AND kind='file'", (order_id,)).fetchone()
        assert job["state"] == "delivered"
        assert job["cleanup_state"] == "completed"
        assert job["local_deleted_at"] is not None

    # Verify local file is completely unlinked from uploads
    assert not photo_path.exists(), "Local photo must be unlinked after delivery confirmation"


# -----------------------------------------------------------------------------
# Scenario 2: Multiple files -> each unlinked independently upon delivery
# -----------------------------------------------------------------------------
def test_multiple_files_unlinked_independently_upon_delivery(isolated_env):
    client, db, uploads = isolated_env
    order_data = create_test_order(client, ("one.jpg", "two.jpg"))
    order_id = order_data["order_id"]

    photos_before = sorted(list(uploads.glob(f"{order_id}_*.jpg")), key=lambda p: p.name)
    assert len(photos_before) == 2

    # Fetch jobs
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        file_jobs = conn.execute(
            "SELECT * FROM delivery_jobs WHERE order_id=? AND kind='file' ORDER BY id", (order_id,)
        ).fetchall()
        assert len(file_jobs) == 2

    job1, job2 = file_jobs[0], file_jobs[1]
    path1 = Path(job1["local_path"])
    path2 = Path(job2["local_path"])

    # Simulate delivering ONLY job1: set state to processing first, then deliver, finish, cleanup
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE delivery_jobs SET state='processing' WHERE id=?", (job1["id"],))

    with patch.object(main, "upload_to_yandex_disk", return_value=True):
        main.deliver_job(dict(job1))
        committed = finish(db, job1["id"], lease_until=None)
        assert committed is True
        perform_job_cleanup(db, job1["id"], uploads_dir=uploads)

    # Job 1 is delivered and cleaned; Job 2 is still pending on disk
    assert not path1.exists(), "Delivered file 1 must be unlinked"
    assert path2.exists(), "Undelivered file 2 must remain on disk"

    with sqlite3.connect(db) as conn:
        j1 = conn.execute("SELECT cleanup_state FROM delivery_jobs WHERE id=?", (job1["id"],)).fetchone()[0]
        j2 = conn.execute("SELECT cleanup_state FROM delivery_jobs WHERE id=?", (job2["id"],)).fetchone()[0]
        assert j1 == "completed"
        assert j2 == "pending"

    # Now deliver Job 2
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE delivery_jobs SET state='processing' WHERE id=?", (job2["id"],))

    with patch.object(main, "upload_to_yandex_disk", return_value=True):
        main.deliver_job(dict(job2))
        committed = finish(db, job2["id"], lease_until=None)
        assert committed is True
        perform_job_cleanup(db, job2["id"], uploads_dir=uploads)

    assert not path2.exists(), "File 2 must now be unlinked as well"


# -----------------------------------------------------------------------------
# Scenario 3: Yandex.Disk error -> local file is preserved
# -----------------------------------------------------------------------------
def test_yandex_disk_error_preserves_local_file(isolated_env):
    client, db, uploads = isolated_env
    order_data = create_test_order(client, ("preserve.jpg",))
    order_id = order_data["order_id"]

    photo_path = list(uploads.glob(f"{order_id}_*.jpg"))[0]
    assert photo_path.is_file()

    # Yandex Disk raises an unhandled network error
    with patch.object(main.httpx, "put", side_effect=RuntimeError("Yandex Cloud Outage")):
        process_due(db, main.deliver_job, uploads_dir=uploads)

    # File MUST still exist on VPS
    assert photo_path.is_file()
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        job = conn.execute("SELECT * FROM delivery_jobs WHERE order_id=? AND kind='file'", (order_id,)).fetchone()
        assert job["state"] == "failed"
        assert job["cleanup_state"] == "pending"
        assert job["local_deleted_at"] is None


# -----------------------------------------------------------------------------
# Scenario 4: SHA-256 mismatch -> deletion is strictly rejected
# -----------------------------------------------------------------------------
def test_sha256_mismatch_prevents_cleanup_and_preserves_file(isolated_env):
    client, db, uploads = isolated_env
    order_data = create_test_order(client, ("corrupt_guard.jpg",))
    order_id = order_data["order_id"]

    photo_path = list(uploads.glob(f"{order_id}_*.jpg"))[0]
    orig_len = len(JPEG)

    # Job is delivered, but before cleanup can delete the file, the local file is modified!
    with sqlite3.connect(db) as conn:
        job = conn.execute("SELECT id FROM delivery_jobs WHERE order_id=? AND kind='file'", (order_id,)).fetchone()
        job_id = job[0]
        conn.execute("UPDATE delivery_jobs SET state='delivered', delivered_at=? WHERE id=?", (utc_now(), job_id))

    # Overwrite file with different content of EXACT same length to trigger SHA-256 mismatch check
    corrupted_data = b"X" * orig_len
    photo_path.write_bytes(corrupted_data)

    # Cleanup must abort and NOT delete the altered file
    success = perform_job_cleanup(db, job_id, uploads_dir=uploads)
    assert success is False
    assert photo_path.is_file(), "File must not be deleted if sha256 changed"

    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT cleanup_state, cleanup_error FROM delivery_jobs WHERE id=?", (job_id,)).fetchone()
        assert row[0] == "failed"
        assert "Sha256MismatchBeforeDelete" in row[1]


# -----------------------------------------------------------------------------
# Scenario 5: SQLite commit error / lost lease -> file preserved
# -----------------------------------------------------------------------------
def test_sqlite_lost_lease_aborts_cleanup(isolated_env):
    client, db, uploads = isolated_env
    order_data = create_test_order(client, ("lease_guard.jpg",))
    order_id = order_data["order_id"]
    photo_path = list(uploads.glob(f"{order_id}_*.jpg"))[0]

    with sqlite3.connect(db) as conn:
        job_id = conn.execute("SELECT id FROM delivery_jobs WHERE order_id=? AND kind='file'", (order_id,)).fetchone()[0]

    # Pretend a worker tried to finish with an invalid/expired lease
    stale_lease = "2020-01-01T00:00:00+00:00"
    committed = finish(db, job_id, lease_until=stale_lease)
    assert committed is False

    # Since delivery commit failed, cleanup must not proceed
    assert photo_path.is_file()
    with sqlite3.connect(db) as conn:
        state = conn.execute("SELECT state FROM delivery_jobs WHERE id=?", (job_id,)).fetchone()[0]
        assert state == "pending"


# -----------------------------------------------------------------------------
# Scenario 6: Cleanup failure (e.g. file lock) -> retried later without reverting delivery state
# -----------------------------------------------------------------------------
def test_cleanup_unlink_failure_retries_without_reverting_delivery(isolated_env):
    client, db, uploads = isolated_env
    order_data = create_test_order(client, ("lock_guard.jpg",))
    order_id = order_data["order_id"]
    photo_path = list(uploads.glob(f"{order_id}_*.jpg"))[0]

    with sqlite3.connect(db) as conn:
        job_id = conn.execute("SELECT id FROM delivery_jobs WHERE order_id=? AND kind='file'", (order_id,)).fetchone()[0]
        conn.execute("UPDATE delivery_jobs SET state='delivered', delivered_at=? WHERE id=?", (utc_now(), job_id))

    # Simulate OS unlink failure (e.g. Windows file lock or permission error)
    with patch.object(Path, "unlink", side_effect=PermissionError("File locked by process")):
        res = perform_job_cleanup(db, job_id, uploads_dir=uploads)
        assert res is False

    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT state, cleanup_state, cleanup_attempts, cleanup_error FROM delivery_jobs WHERE id=?", (job_id,)).fetchone()
        assert row[0] == "delivered", "Delivery state must NOT revert to pending!"
        assert row[1] == "failed"
        assert row[2] == 1
        assert "UnlinkError" in row[3]

    assert photo_path.is_file()

    # Retry cleanup on next iteration (file lock released)
    cleaned = process_due_cleanups(db, uploads_dir=uploads)
    assert cleaned == 1
    assert not photo_path.exists()

    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT state, cleanup_state, local_deleted_at FROM delivery_jobs WHERE id=?", (job_id,)).fetchone()
        assert row[0] == "delivered"
        assert row[1] == "completed"
        assert row[2] is not None


# -----------------------------------------------------------------------------
# Scenario 7: Restart after delivery before cleanup -> cleanup recovered
# -----------------------------------------------------------------------------
def test_restart_after_delivery_before_cleanup_recovers(isolated_env):
    client, db, uploads = isolated_env
    order_data = create_test_order(client, ("crash_recovery.jpg",))
    order_id = order_data["order_id"]
    photo_path = list(uploads.glob(f"{order_id}_*.jpg"))[0]

    with sqlite3.connect(db) as conn:
        job_id = conn.execute("SELECT id FROM delivery_jobs WHERE order_id=? AND kind='file'", (order_id,)).fetchone()[0]
        # Delivery was committed, but system crashed before cleanup could be executed
        conn.execute("UPDATE delivery_jobs SET state='delivered', cleanup_state='pending', delivered_at=? WHERE id=?", (utc_now(), job_id))

    assert photo_path.is_file()

    # System restarts: sweep recovers and cleans
    cleaned = process_due_cleanups(db, uploads_dir=uploads)
    assert cleaned == 1
    assert not photo_path.exists()

    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT cleanup_state, local_deleted_at FROM delivery_jobs WHERE id=?", (job_id,)).fetchone()
        assert row[0] == "completed"
        assert row[1] is not None


# -----------------------------------------------------------------------------
# Scenario 8: Repeated processing -> idempotent, no re-upload
# -----------------------------------------------------------------------------
def test_repeated_processing_is_idempotent(isolated_env):
    client, db, uploads = isolated_env
    order_data = create_test_order(client, ("repeat.jpg",))
    order_id = order_data["order_id"]

    # Mark ALL jobs for this order as delivered & cleaned
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE delivery_jobs SET state='delivered', cleanup_state='completed', cleanup_phase='deleted', local_deleted_at=? WHERE order_id=?",
            (utc_now(), order_id),
        )

    # File is unlinked
    for f in list(uploads.glob(f"{order_id}_*")):
        f.unlink()

    # Fetch file job id
    with sqlite3.connect(db) as conn:
        job_id = conn.execute("SELECT id FROM delivery_jobs WHERE order_id=? AND kind='file'", (order_id,)).fetchone()[0]

    # Calling perform_job_cleanup again must be a safe, idempotent no-op
    res = perform_job_cleanup(db, job_id, uploads_dir=uploads)
    assert res is True

    # Claim due should find no pending jobs
    claimed = claim_due(db)
    assert claimed is None


# -----------------------------------------------------------------------------
# Scenario 9: Expired lease -> deletion not performed by foreign / stale worker
# -----------------------------------------------------------------------------
def test_expired_lease_cleanup_not_performed_by_stale_worker(isolated_env):
    client, db, uploads = isolated_env
    order_data = create_test_order(client, ("lease_collision.jpg",))
    order_id = order_data["order_id"]
    photo_path = list(uploads.glob(f"{order_id}_*.jpg"))[0]

    with sqlite3.connect(db) as conn:
        job_id = conn.execute("SELECT id FROM delivery_jobs WHERE order_id=? AND kind='file'", (order_id,)).fetchone()[0]
        # Active cleanup lease held by Worker 1 until 1 hour in the future
        future_lease = "2099-01-01T00:00:00+00:00"
        conn.execute(
            "UPDATE delivery_jobs SET state='delivered', cleanup_state='processing', cleanup_lease_until=? WHERE id=?",
            (future_lease, job_id),
        )

    # Worker 2 attempts to perform cleanup while Worker 1 holds active lease -> rejected
    res = perform_job_cleanup(db, job_id, uploads_dir=uploads)
    assert res is False
    assert photo_path.is_file()


# -----------------------------------------------------------------------------
# Scenario 10: Attempted deletion outside uploads directory -> rejected
# -----------------------------------------------------------------------------
def test_path_outside_uploads_rejected(isolated_env, tmp_path):
    client, db, uploads = isolated_env
    outside_file = tmp_path / "secret.txt"
    outside_file.write_bytes(b"sensitive-system-data")

    # Manually inject malicious delivery job pointing outside uploads
    job_id = "malicious-job-1"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO delivery_jobs (id, order_id, kind, local_path, content_sha256, content_size, "
            "state, cleanup_state, next_attempt_at, created_at) "
            "VALUES (?, 1, 'file', ?, ?, ?, 'delivered', 'pending', ?, ?)",
            (job_id, str(outside_file), hashlib.sha256(b"sensitive-system-data").hexdigest(), len(b"sensitive-system-data"), utc_now(), utc_now()),
        )

    res = perform_job_cleanup(db, job_id, uploads_dir=uploads)
    assert res is False
    assert outside_file.is_file(), "File outside uploads must NEVER be deleted"

    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT cleanup_state, cleanup_error FROM delivery_jobs WHERE id=?", (job_id,)).fetchone()
        assert row[0] == "failed"
        assert row[1] == "PathOutsideUploads"


# -----------------------------------------------------------------------------
# Scenario 11: Symlink or file substitution -> rejected
# -----------------------------------------------------------------------------
def test_symlink_rejected(isolated_env, tmp_path):
    client, db, uploads = isolated_env
    target = tmp_path / "target.jpg"
    target.write_bytes(JPEG)
    symlink_path = uploads / "symlink_attack.jpg"

    try:
        symlink_path.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks not permitted in this OS environment")

    job_id = "symlink-job-1"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO delivery_jobs (id, order_id, kind, local_path, content_sha256, content_size, "
            "state, cleanup_state, next_attempt_at, created_at) "
            "VALUES (?, 1, 'file', ?, ?, ?, 'delivered', 'pending', ?, ?)",
            (job_id, str(symlink_path), hashlib.sha256(JPEG).hexdigest(), len(JPEG), utc_now(), utc_now()),
        )

    res = perform_job_cleanup(db, job_id, uploads_dir=uploads)
    assert res is False

    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT cleanup_state, cleanup_error FROM delivery_jobs WHERE id=?", (job_id,)).fetchone()
        assert row[0] == "failed"
        assert row[1] == "SymlinkDetected"


# -----------------------------------------------------------------------------
# Scenario 12: info.txt unlinked after delivery confirmed
# -----------------------------------------------------------------------------
def test_info_txt_unlinked_after_delivery_confirmed(isolated_env):
    client, db, uploads = isolated_env
    order_data = create_test_order(client, ("doc.jpg",))
    order_id = order_data["order_id"]

    info_files = list(uploads.glob(f"{order_id}_*_info.txt"))
    assert len(info_files) == 1
    info_path = info_files[0]
    assert info_path.is_file()

    with sqlite3.connect(db) as conn:
        info_job = conn.execute("SELECT id FROM delivery_jobs WHERE order_id=? AND kind='info'", (order_id,)).fetchone()
        info_job_id = info_job[0]
        conn.execute("UPDATE delivery_jobs SET state='processing' WHERE id=?", (info_job_id,))

    with patch.object(main, "upload_info_to_yandex_disk", return_value=True):
        main.deliver_job({"order_id": order_id, "kind": "info", "local_path": str(info_path), "remote_path": "Orders/info.txt"})
        committed = finish(db, info_job_id, lease_until=None)
        assert committed is True
        perform_job_cleanup(db, info_job_id, uploads_dir=uploads)

    assert not info_path.exists(), "info.txt must be unlinked from uploads after delivery confirmed"
    with sqlite3.connect(db) as conn:
        cleanup_state = conn.execute("SELECT cleanup_state FROM delivery_jobs WHERE id=?", (info_job_id,)).fetchone()[0]
        assert cleanup_state == "completed"


# -----------------------------------------------------------------------------
# Scenario 13: Backup verifier accepts cleanly delivered & cleaned orders
# -----------------------------------------------------------------------------
def test_backup_verifier_accepts_cleanly_delivered_and_cleaned_orders(isolated_env):
    client, db, uploads = isolated_env
    order_data = create_test_order(client, ("photo1.jpg", "photo2.jpg"))
    order_id = order_data["order_id"]

    # Mark all file & info jobs as delivered and cleaned
    with sqlite3.connect(db) as conn:
        deliv_ts = "2026-10-27T10:00:00+00:00"
        deleted_ts = "2026-10-27T10:05:00+00:00"
        conn.execute(
            "UPDATE delivery_jobs SET state='delivered', cleanup_state='completed', cleanup_phase='deleted', "
            "delivered_at=?, local_deleted_at=? WHERE order_id=?",
            (deliv_ts, deleted_ts, order_id),
        )

    # Delete all local files
    for f in list(uploads.glob(f"{order_id}_*")):
        f.unlink()

    assert len(list(uploads.glob(f"{order_id}_*"))) == 0

    is_valid, report = verify_backup(db, uploads)
    assert is_valid is True
    assert report["p1_orders_count"] == 1
    assert report["p1_cleaned_files"] == 2
    assert report["p1_cleaned_info"] == 1
    assert len(report["p1_missing_files"]) == 0
    assert len(report["p1_delivered_uncleaned_anomalies"]) == 0


# -----------------------------------------------------------------------------
# Scenario 14: Backup verifier rejects loss of undelivered or uncleaned files
# -----------------------------------------------------------------------------
def test_backup_verifier_rejects_loss_of_undelivered_files(isolated_env):
    client, db, uploads = isolated_env
    order_data = create_test_order(client, ("lost.jpg",))
    order_id = order_data["order_id"]

    # Files are still pending in delivery_jobs, but local file is deleted unexpectedly!
    photo_file = list(uploads.glob(f"{order_id}_*.jpg"))[0]
    photo_file.unlink()

    is_valid, report = verify_backup(db, uploads)
    assert is_valid is False
    assert len(report["p1_missing_files"]) == 1
    assert report["p1_missing_files"][0]["state"] == "pending"


def test_backup_verifier_rejects_missing_file_when_cleanup_not_completed(isolated_env):
    client, db, uploads = isolated_env
    order_data = create_test_order(client, ("anomaly.jpg",))
    order_id = order_data["order_id"]

    # File is marked delivered, but cleanup is NOT completed (e.g. pending/failed)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE delivery_jobs SET state='delivered', cleanup_state='pending', delivered_at=? WHERE order_id=?",
            (utc_now(), order_id),
        )

    # Local file is missing before cleanup confirmation
    photo_file = list(uploads.glob(f"{order_id}_*.jpg"))[0]
    photo_file.unlink()

    is_valid, report = verify_backup(db, uploads)
    assert is_valid is False
    assert len(report["p1_delivered_uncleaned_anomalies"]) == 1
    assert any("cleanup is 'pending', yet local file is missing" in e for e in report["errors"])


# -----------------------------------------------------------------------------
# Scenario 15: Payments, emails, and order queries work without local files
# -----------------------------------------------------------------------------
def test_payments_and_emails_work_without_local_photos(isolated_env):
    client, db, uploads = isolated_env
    order_data = create_test_order(client, ("photo1.jpg", "photo2.jpg"))
    order_id = order_data["order_id"]
    order_token = order_data["order_token"]

    # Deliver and clean all files from disk
    with sqlite3.connect(db) as conn:
        deliv_ts = "2026-10-27T10:00:00+00:00"
        deleted_ts = "2026-10-27T10:05:00+00:00"
        conn.execute(
            "UPDATE delivery_jobs SET state='delivered', cleanup_state='completed', cleanup_phase='deleted', "
            "delivered_at=?, local_deleted_at=? WHERE order_id=?",
            (deliv_ts, deleted_ts, order_id),
        )

    for f in list(uploads.glob(f"{order_id}_*")):
        f.unlink()

    # Uploads directory is now completely empty
    assert len(list(uploads.glob("*"))) == 0

    # 1. Payment creation endpoint succeeds and authoritative price is computed from orders table
    pay_resp = client.post(
        "/api/payment/create",
        json={"order_id": order_id, "order_token": order_token, "amount": "40.00"},
    )
    assert pay_resp.status_code == 200, pay_resp.text
    pay_data = pay_resp.json()
    assert pay_data["amount"] == "40.00"
    assert "payment_url" in pay_data

    # 2. Transition order to paid status succeeds
    paid_ok = main.mark_order_paid(order_id, "40.00")
    assert paid_ok is True

    # 3. Order query succeeds
    order = main.get_order_by_id(order_id)
    assert order["payment_status"] == "paid"
    assert order["status"] == "paid"

    # 4. Paid order email succeeds without needing local photo files
    with patch("main.smtplib.SMTP_SSL") as mock_smtp:
        mock_instance = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_instance
        os.environ["SMTP_SERVER"] = "smtp.example.invalid"
        os.environ["SMTP_USER"] = "user@example.invalid"
        os.environ["SMTP_PASSWORD"] = "pass"
        os.environ["EMAIL_TO"] = "lab@example.invalid"
        email_ok = main.send_paid_order_email(order_id)
        assert email_ok is True
        assert mock_instance.send_message.called


# -----------------------------------------------------------------------------
# Scenario 16: Safe cleanup of stale temporary (.incoming-*) files
# -----------------------------------------------------------------------------
def test_temporary_files_cleanup_preserves_orders_and_prunes_stale_incoming(isolated_env):
    client, db, uploads = isolated_env

    # 1. Real order file
    order_data = create_test_order(client, ("real.jpg",))
    order_id = order_data["order_id"]
    real_file = list(uploads.glob(f"{order_id}_*.jpg"))[0]

    # Artificially age the real order file (2 hours old)
    old_time = time.time() - 7200
    os.utime(real_file, (old_time, old_time))

    # 2. Orphaned .incoming- file from crashed upload (2 hours old)
    stale_incoming = uploads / ".incoming-abandoned123"
    stale_incoming.write_bytes(b"partial-data")
    os.utime(stale_incoming, (old_time, old_time))

    # 3. Fresh .incoming- file (in-flight upload, 10 seconds old)
    fresh_incoming = uploads / ".incoming-activeupload456"
    fresh_incoming.write_bytes(b"in-flight-data")

    # Run temporary file cleaner with 3600s threshold
    removed_count = cleanup_orphaned_temporary_files(uploads, db, max_age_seconds=3600)
    assert removed_count == 1

    # Stale .incoming- was deleted
    assert not stale_incoming.exists()

    # Fresh .incoming- was preserved
    assert fresh_incoming.exists()

    # Real order file was strictly PRESERVED (never deleted by age alone!)
    assert real_file.is_file()


# -----------------------------------------------------------------------------
# Scenario 17: Worker lease fencing concurrency
# Worker A loses lease, Worker B acquires it -> Worker A cannot delete source or overwrite state
# -----------------------------------------------------------------------------
def test_stale_worker_cannot_delete_or_overwrite_state_after_lease_loss(isolated_env):
    """
    Proves that a stale worker whose lease expired and was acquired by a newer worker
    CANNOT unlink the file and CANNOT overwrite the newer worker's state.
    """
    client, db, uploads = isolated_env
    order_data = create_test_order(client, ("fencing_guard.jpg",))
    order_id = order_data["order_id"]
    photo_path = list(uploads.glob(f"{order_id}_*.jpg"))[0]

    with sqlite3.connect(db) as conn:
        job_id = conn.execute("SELECT id FROM delivery_jobs WHERE order_id=? AND kind='file'", (order_id,)).fetchone()[0]
        conn.execute("UPDATE delivery_jobs SET state='delivered', delivered_at=? WHERE id=?", (utc_now(), job_id))

    # Worker A claims lease
    past_time = "2026-01-01T00:00:00+00:00"
    token_A = f"{past_time}#1#workerA123"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE delivery_jobs SET cleanup_state='processing', cleanup_lease_until=?, cleanup_attempts=1 WHERE id=?",
            (token_A, job_id),
        )

    from datetime import datetime, timedelta, timezone
    # Worker B arrives after Worker A's lease expired and acquires the lease
    future_time = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    token_B = f"{future_time}#2#workerB456"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE delivery_jobs SET cleanup_state='processing', cleanup_lease_until=?, cleanup_attempts=2 WHERE id=?",
            (token_B, job_id),
        )

    from delivery import _is_cleanup_lease_valid, _mark_cleanup_completed, _mark_cleanup_failed

    # 1. Stale Worker A verifies its lease: MUST FAIL
    with sqlite3.connect(db) as conn:
        assert _is_cleanup_lease_valid(conn, job_id, token_A) is False

    # 2. Stale Worker A tries to mark completed: MUST BE REJECTED (rowcount == 0)
    res_completed_a = _mark_cleanup_completed(db, job_id, token_A)
    assert res_completed_a is False

    # 3. Stale Worker A tries to mark failed: MUST BE REJECTED (rowcount == 0)
    res_failed_a = _mark_cleanup_failed(db, job_id, token_A, error="StaleWorkerError")
    assert res_failed_a is False

    # Photo file must still exist unharmed
    assert photo_path.is_file()

    # 4. Active Worker B verifies its lease: MUST SUCCEED
    with sqlite3.connect(db) as conn:
        assert _is_cleanup_lease_valid(conn, job_id, token_B) is True

    # 5. Active Worker B unlinks and completes
    photo_path.unlink()
    res_completed_b = _mark_cleanup_completed(db, job_id, token_B)
    assert res_completed_b is True

    # 6. Verify final DB state is cleanly completed by Worker B, no pollution from Worker A
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT cleanup_state, cleanup_error, cleanup_attempts FROM delivery_jobs WHERE id=?", (job_id,)).fetchone()
        assert row[0] == "completed"
        assert row[1] is None, "Worker A's stale error was not written"
        assert row[2] == 2, "Reflects Worker B's generation"


def test_perform_job_cleanup_aborts_deletion_if_lease_stolen_concurrently(isolated_env):
    """
    Deterministic concurrency regression test proving that:
    1. Worker A obtains a valid lease and reaches the deletion boundary.
    2. Worker A pauses before the filesystem mutation (via pre_unlink_hook).
    3. Worker B attempts to acquire / acquires the lease concurrently.
    4. The write transaction fencing prevents stale deletion: Worker A aborts without unlinking.
    5. Worker B's lease/state is not overwritten by Worker A.
    6. The source file remains intact on disk until Worker B legitimately performs cleanup.
    """
    client, db, uploads = isolated_env
    order_data = create_test_order(client, ("race_guard.jpg",))
    order_id = order_data["order_id"]
    photo_path = list(uploads.glob(f"{order_id}_*.jpg"))[0]
    assert photo_path.is_file()

    with sqlite3.connect(db) as conn:
        job_id = conn.execute(
            "SELECT id FROM delivery_jobs WHERE order_id=? AND kind='file'", (order_id,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE delivery_jobs SET state='delivered', delivered_at=? WHERE id=?",
            (utc_now(), job_id),
        )

    worker_a_paused = threading.Event()
    worker_b_stole_lease = threading.Event()
    worker_a_result = {}
    unlink_called = []

    real_unlink = Path.unlink

    def tracking_unlink(self):
        unlink_called.append(str(self))
        return real_unlink(self)

    def hook_a(jid, token):
        worker_a_paused.set()
        assert worker_b_stole_lease.wait(timeout=5.0), "Worker B timed out stealing lease"

    def run_worker_a():
        with patch.object(Path, "unlink", tracking_unlink):
            worker_a_result["success"] = perform_job_cleanup(
                db, job_id, uploads_dir=uploads, pre_unlink_hook=hook_a
            )

    thread_a = threading.Thread(target=run_worker_a)
    thread_a.start()

    # 1 & 2. Wait for Worker A to reach deletion boundary and pause
    assert worker_a_paused.wait(timeout=5.0), "Worker A should pause at deletion boundary"

    # Verify Worker A currently holds the lease and durably recorded 'deleting' intent
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT cleanup_state, cleanup_phase, cleanup_lease_until FROM delivery_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        assert row[0] == "processing"
        assert row[1] == "deleting"
        token_a = row[2]
        assert token_a is not None

    # 3. Worker B acquires the lease concurrently (new token with valid future expiration)
    token_b = f"{(datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()}#99#workerB_nonce"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE delivery_jobs SET cleanup_state='processing', cleanup_lease_until=? WHERE id=?",
            (token_b, job_id),
        )

    # 4. Resume Worker A
    worker_b_stole_lease.set()
    thread_a.join(timeout=5.0)
    assert not thread_a.is_alive(), "Worker A thread should terminate"

    # Worker A must abort without mutation and return False
    assert worker_a_result.get("success") is False, "Worker A must return False on stolen lease"
    assert len(unlink_called) == 0, "Worker A must NOT call unlink() after lease theft!"

    # 5. Worker B's lease token was NOT overwritten by Worker A
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT cleanup_state, cleanup_lease_until FROM delivery_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        assert row[0] == "processing"
        assert row[1] == token_b, "Worker B's lease must remain intact without overwrite by Worker A"

    # 6. Source file remains intact until Worker B legitimately performs cleanup
    assert photo_path.is_file(), "File must remain intact until legitimate owner deletes it"

    # Clear lease so Worker B can claim and process
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE delivery_jobs SET cleanup_state='pending', cleanup_lease_until=NULL WHERE id=?",
            (job_id,),
        )

    res_b = perform_job_cleanup(db, job_id, uploads_dir=uploads)
    assert res_b is True, "Worker B must legitimately succeed"
    assert not photo_path.exists(), "File should now be unlinked by legitimate Worker B"

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT cleanup_state, cleanup_phase, local_deleted_at FROM delivery_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        assert row[0] == "completed"
        assert row[1] == "deleted"
        assert row[2] is not None


def test_real_second_worker_lease_acquisition_and_fencing(isolated_env):
    """
    R3.3 Integration Concurrency Test:
    Demonstrates that Worker B acquires an expired job via real production lease-claim logic,
    and Worker A aborts without deleting the source or overwriting Worker B's state.

    1. Worker A acquires lease and commits durable intent 'deleting' at t=0.
    2. Worker A pauses before filesystem mutation.
    3. Simulate lease expiry deterministically using injectable clock seam (t=15s > lease_duration=10s).
    4. Worker B invokes perform_job_cleanup() through normal lease-claim logic and acquires the job.
    5. Worker A resumes and aborts without unlinking or overwriting Worker B's lease.
    6. Worker B resumes and completes cleanup using its own valid lease.
    7. File is legitimately deleted and final database state is 'completed'.
    """
    client, db, uploads = isolated_env
    order_data = create_test_order(client, ("real_race.jpg",))
    order_id = order_data["order_id"]
    photo_path = list(uploads.glob(f"{order_id}_*.jpg"))[0]
    assert photo_path.is_file()

    with sqlite3.connect(db) as conn:
        job_id = conn.execute(
            "SELECT id FROM delivery_jobs WHERE order_id=? AND kind='file'", (order_id,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE delivery_jobs SET state='delivered', delivered_at='2026-10-27T12:00:00+00:00' WHERE id=?",
            (job_id,),
        )

    t0_str = "2026-10-27T12:00:00+00:00"
    t1_str = "2026-10-27T12:00:15+00:00"  # 15s later -> 10s lease is expired

    worker_a_paused = threading.Event()
    worker_b_claimed = threading.Event()
    worker_b_can_delete = threading.Event()

    worker_a_result = {}
    worker_b_result = {}
    unlink_calls = []

    real_unlink = Path.unlink

    def tracking_unlink(self):
        unlink_calls.append(str(self))
        return real_unlink(self)

    def hook_a(jid, token):
        worker_a_paused.set()
        assert worker_b_claimed.wait(timeout=5.0), "Worker B timed out claiming lease"

    def hook_b(jid, token):
        worker_b_claimed.set()
        assert worker_b_can_delete.wait(timeout=5.0), "Worker B timed out waiting to delete"

    def run_worker_a():
        worker_a_result["success"] = perform_job_cleanup(
            db,
            job_id,
            uploads_dir=uploads,
            pre_unlink_hook=hook_a,
            clock=lambda: t0_str,
            lease_duration_seconds=10,
        )

    def run_worker_b():
        worker_b_result["success"] = perform_job_cleanup(
            db,
            job_id,
            uploads_dir=uploads,
            pre_unlink_hook=hook_b,
            clock=lambda: t1_str,
            lease_duration_seconds=120,
        )

    with patch.object(Path, "unlink", tracking_unlink):
        thread_a = threading.Thread(target=run_worker_a)
        thread_b = threading.Thread(target=run_worker_b)

        # 1 & 2: Start Worker A and wait for it to pause at deletion boundary
        thread_a.start()
        assert worker_a_paused.wait(timeout=5.0), "Worker A should pause at hook"

        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT cleanup_state, cleanup_phase, cleanup_lease_until, cleanup_attempts FROM delivery_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            assert row[0] == "processing"
            assert row[1] == "deleting"
            assert row[3] == 1
            token_a = row[2]

        # 3 & 4: Start Worker B. Worker B sees t1_str > lease_ts of Worker A, claims lease via real code!
        thread_b.start()
        assert worker_b_claimed.wait(timeout=5.0), "Worker B should legitimately claim expired lease and pause"

        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT cleanup_state, cleanup_phase, cleanup_lease_until, cleanup_attempts FROM delivery_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            assert row[0] == "processing"
            assert row[1] == "deleting"
            assert row[3] == 2, "Worker B incremented attempt counter via legitimate lease claim"
            token_b = row[2]
            assert token_b != token_a, "Worker B must have acquired its own unique lease token"

        # 5: Worker A resumes and aborts
        thread_a.join(timeout=5.0)
        assert not thread_a.is_alive(), "Worker A should finish"
        assert worker_a_result.get("success") is False, "Worker A must return False on lost lease"
        assert len(unlink_calls) == 0, "Worker A must NOT have unlinked the file!"

        # Worker B's lease was NOT overwritten by Worker A
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT cleanup_state, cleanup_lease_until FROM delivery_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            assert row[0] == "processing"
            assert row[1] == token_b, "Worker B's lease token was untouched"

        assert photo_path.is_file(), "File must still be on disk before Worker B unlinks it"

        # 6: Signal Worker B to proceed with legitimate cleanup
        worker_b_can_delete.set()
        thread_b.join(timeout=5.0)
        assert not thread_b.is_alive(), "Worker B should finish"
        assert worker_b_result.get("success") is True, "Worker B must return True on successful cleanup"

        # 7: File was unlinked exactly once and database state is completed
        assert not photo_path.exists(), "File should now be unlinked by Worker B"
        assert len(unlink_calls) == 1, "unlink() must have been called exactly once (by Worker B)"

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT cleanup_state, cleanup_phase, local_deleted_at, cleanup_lease_until FROM delivery_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        assert row[0] == "completed"
        assert row[1] == "deleted"
        assert row[2] == t1_str
        assert row[3] is None


def test_perform_job_cleanup_missing_local_path_fails_closed(isolated_env):
    """
    R3.3 Defect 1: A missing local_path on a delivered file job must NOT become completed.
    Must record an explicit anomaly MissingLocalPath without fabricated local_deleted_at.
    """
    client, db, uploads = isolated_env
    order_data = create_test_order(client, ("missing_path.jpg",))
    order_id = order_data["order_id"]

    with sqlite3.connect(db) as conn:
        job_id = conn.execute(
            "SELECT id FROM delivery_jobs WHERE order_id=? AND kind='file'", (order_id,)
        ).fetchone()[0]
        # Simulate corrupted or missing local_path
        conn.execute(
            "UPDATE delivery_jobs SET state='delivered', local_path=NULL, delivered_at=? WHERE id=?",
            (utc_now(), job_id),
        )

    res = perform_job_cleanup(db, job_id, uploads_dir=uploads)
    assert res is False, "Must fail when local_path is missing"

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT cleanup_state, cleanup_phase, cleanup_error, local_deleted_at FROM delivery_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        assert row[0] == "failed", "Must be marked failed"
        assert row[1] == "failed"
        assert row[2] == "MissingLocalPath", "Explicit anomaly recorded"
        assert row[3] is None, "Must NOT fabricate local_deleted_at"

    # Must NOT be retried or converted to completed by process_due_cleanups
    cleaned = process_due_cleanups(db, uploads_dir=uploads)
    assert cleaned == 0

    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT cleanup_state FROM delivery_jobs WHERE id=?", (job_id,)).fetchone()
        assert row[0] == "failed"


def test_perform_job_cleanup_crash_recovery_stolen_lease_returns_false(isolated_env):
    """
    R3.3 Defect 2: When recovery finalizes an already absent source (cleanup_phase=='deleting'),
    return success ONLY if conditional database finalization succeeds. If lease was stolen, return False.
    """
    client, db, uploads = isolated_env
    order_data = create_test_order(client, ("stolen_rec.jpg",))
    order_id = order_data["order_id"]
    photo_path = list(uploads.glob(f"{order_id}_*.jpg"))[0]
    photo_path.unlink()  # File is absent

    with sqlite3.connect(db) as conn:
        job_id = conn.execute(
            "SELECT id FROM delivery_jobs WHERE order_id=? AND kind='file'", (order_id,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE delivery_jobs SET state='delivered', cleanup_state='failed', cleanup_phase='deleting', "
            "delivered_at=? WHERE id=?",
            (utc_now(), job_id),
        )

    # Intercept _mark_cleanup_completed to simulate lease theft right before finalization
    from delivery import _mark_cleanup_completed as real_mark_completed

    def stolen_lease_mark_completed(db_path, jid, token=None):
        # Worker B stole lease
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE delivery_jobs SET cleanup_lease_until='stolen#99#workerB' WHERE id=?",
                (jid,),
            )
        # Attempt real conditional update with Worker A's token
        return real_mark_completed(db_path, jid, token)

    with patch("delivery._mark_cleanup_completed", side_effect=stolen_lease_mark_completed):
        res = perform_job_cleanup(db, job_id, uploads_dir=uploads)
        assert res is False, "Must return False when conditional finalization update returns False!"

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT cleanup_state, cleanup_lease_until FROM delivery_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        assert row[0] == "processing"
        assert row[1] == "stolen#99#workerB", "Worker B's lease was not overwritten"
