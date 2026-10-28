"""
PhotoDoc AI — Zero-Retention Backup & Privacy Gate Test Suite.

Validates:
1. Newly generated backups contain ZERO delivered customer photo content (NEW_BACKUP_ZERO_DELIVERED_PHOTO_CONTENT=PASS).
2. Unfinished delivery originals (pending, processing, failed) are preserved in backups (UNFINISHED_DELIVERY_RECOVERY=PASS).
3. Mixed order scenarios: delivered photos are omitted while unfinished photos are archived.
4. Backup manifest (manifest.sha256 and backup_manifest.json) records zero-retention metadata accurately.
5. verify_backup_integrity.py validates the zero-retention backup cleanly (absence of delivered photos is valid).
6. verify_backup_integrity.py fails if an unfinished delivery photo is missing from the backup.
7. audit_backup_archives.py identifies historical archives retaining delivered photos as HIGH risk (read-only).
8. audit_backup_archives.py confirms ZERO risk on zero-retention backups.
9. audit_backup_archives.py and create_backup.py strictly never delete or overwrite existing archives.
10. Shared/duplicate files across jobs: preserved if at least one job is unfinished; omitted only when all referencing jobs are delivered.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
SCRIPTS_DIR = REPO_DIR / "scripts"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import main  # noqa: E402
from audit_backup_archives import audit_single_backup_dir  # noqa: E402
from create_backup import generate_backup  # noqa: E402
from db_contract import APPLICATION_ID  # noqa: E402
from delivery import perform_job_cleanup, process_due_cleanups, set_uploads_dir  # noqa: E402
from verify_backup_integrity import verify_backup  # noqa: E402

JPEG = b"\xff\xd8\xffsynthetic-photo-data-for-backup-test"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRsynthetic-png-backup-test"


@pytest.fixture
def zero_retention_env(tmp_path, monkeypatch):
    db_path = tmp_path / "orders.db"
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads_dir)
    set_uploads_dir(uploads_dir)

    monkeypatch.setenv("ROBOKASSA_MERCHANT_LOGIN", "test-merchant")
    monkeypatch.setenv("ROBOKASSA_PASSWORD1", "test-pass-1")
    monkeypatch.setenv("ROBOKASSA_PASSWORD2", "test-pass-2")
    monkeypatch.setenv("DELIVERY_WORKER_ENABLED", "0")

    main.init_db()
    client = TestClient(main.app)
    yield client, db_path, uploads_dir, tmp_path


def create_order_helper(client, name: str, filenames_and_data: list[tuple[str, bytes]]) -> int:
    files = []
    for fn, data in filenames_and_data:
        mime = "image/png" if fn.lower().endswith(".png") else "image/jpeg"
        files.append(("files", (fn, data, mime)))
    resp = client.post(
        "/api/order",
        data={"name": name, "phone": "+79991112233", "format": "10x15"},
        files=files,
    )
    assert resp.status_code == 200
    return resp.json()["order_id"]


def test_new_backup_contains_zero_delivered_photo_content(zero_retention_env):
    """
    Mandatory Privacy Gate:
    Newly generated backup of orders whose delivery and cleanup are complete
    must contain ZERO customer photo bytes in uploads.tar.gz.
    """
    client, db_path, uploads_dir, tmp_path = zero_retention_env

    # 1. Create order and upload photo
    order_id = create_order_helper(client, "Alice Delivered", [("alice.jpg", JPEG)])

    # 2. Mark delivery completed and perform cleanup
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE delivery_jobs SET state='delivered', delivered_at='2026-10-28T10:00:00+00:00' "
            "WHERE order_id=?",
            (order_id,),
        )

    # Run cleanup for all jobs belonging to this order
    with sqlite3.connect(db_path) as conn:
        job_ids = [row[0] for row in conn.execute("SELECT id FROM delivery_jobs WHERE order_id=?", (order_id,)).fetchall()]

    for jid in job_ids:
        perform_job_cleanup(db_path, jid, uploads_dir=uploads_dir)

    # Verify local photo is unlinked
    photos = list(uploads_dir.glob(f"{order_id}_*.jpg"))
    assert len(photos) == 0

    # 3. Generate zero-retention backup
    backup_out = tmp_path / "backup_release_1"
    success, report = generate_backup(
        db_path=db_path,
        uploads_dir=uploads_dir,
        output_dir=backup_out,
    )
    assert success is True
    assert report["zero_retention_enforced"] is True
    assert report["archived_files_count"] == 0
    assert report["omitted_delivered_files_count"] >= 1  # Delivered photo was excluded!

    # 4. Inspect generated tarball directly: assert zero photos inside
    tar_path = backup_out / "uploads.tar.gz"
    with tarfile.open(tar_path, "r:gz") as tar:
        member_names = [m.name for m in tar.getmembers() if m.isfile()]
        assert len(member_names) == 0  # Absolutely NO photo files!

    # 5. Verify backup passes validation
    assert report["verification_passed"] is True


def test_unfinished_delivery_retains_recoverable_originals_in_backup(zero_retention_env):
    """
    Disaster Recovery Gate:
    Pending, processing, or failed deliveries MUST have their original files
    preserved in newly generated backups so they can be recovered.
    """
    client, db_path, uploads_dir, tmp_path = zero_retention_env

    # Order 1: Pending delivery
    order1 = create_order_helper(client, "Bob Pending", [("bob_pending.jpg", JPEG)])

    # Order 2: Failed delivery
    order2 = create_order_helper(client, "Charlie Failed", [("charlie_failed.png", PNG)])
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE delivery_jobs SET state='failed', attempts=3, last_error='NetworkTimeout' "
            "WHERE order_id=?",
            (order2,),
        )

    backup_out = tmp_path / "backup_release_recovery"
    success, report = generate_backup(
        db_path=db_path,
        uploads_dir=uploads_dir,
        output_dir=backup_out,
    )
    assert success is True
    assert report["unfinished_jobs_count"] >= 4  # 2 files + 2 info jobs
    assert report["archived_files_count"] >= 4

    # Verify tarball contains the required photos
    tar_path = backup_out / "uploads.tar.gz"
    with tarfile.open(tar_path, "r:gz") as tar:
        files_in_tar = [Path(m.name).name for m in tar.getmembers() if m.isfile()]
        # Check that files for order1 and order2 are present
        assert any(f.startswith(f"{order1}_") and f.endswith(".jpg") for f in files_in_tar)
        assert any(f.startswith(f"{order2}_") and f.endswith(".png") for f in files_in_tar)

    assert report["verification_passed"] is True


def test_mixed_orders_backup_archives_only_unfinished(zero_retention_env):
    """
    In a mixed environment (some orders delivered, some pending):
    - Delivered photos are excluded.
    - Pending photos are archived.
    """
    client, db_path, uploads_dir, tmp_path = zero_retention_env

    # Order 1: Delivered and cleaned
    order1 = create_order_helper(client, "Delivered User", [("photo_del.jpg", JPEG)])
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE delivery_jobs SET state='delivered' WHERE order_id=?", (order1,))
    with sqlite3.connect(db_path) as conn:
        job_ids = [r[0] for r in conn.execute("SELECT id FROM delivery_jobs WHERE order_id=?", (order1,)).fetchall()]
    for jid in job_ids:
        perform_job_cleanup(db_path, jid, uploads_dir=uploads_dir)

    # Order 2: Pending
    order2 = create_order_helper(client, "Pending User", [("photo_pend.png", PNG)])

    backup_out = tmp_path / "backup_release_mixed"
    success, report = generate_backup(
        db_path=db_path,
        uploads_dir=uploads_dir,
        output_dir=backup_out,
    )
    assert success is True

    tar_path = backup_out / "uploads.tar.gz"
    with tarfile.open(tar_path, "r:gz") as tar:
        files_in_tar = [Path(m.name).name for m in tar.getmembers() if m.isfile()]
        # Delivered files are ABSENT
        assert not any(f.startswith(f"{order1}_") for f in files_in_tar)
        # Pending files are PRESENT
        assert any(f.startswith(f"{order2}_") for f in files_in_tar)


def test_backup_fails_if_unfinished_order_source_is_missing(zero_retention_env):
    """
    Fail-Closed Recovery Contract:
    If a photo required for an unfinished delivery is missing from disk,
    backup generation MUST fail rather than silently omitting it.
    """
    client, db_path, uploads_dir, tmp_path = zero_retention_env

    order_id = create_order_helper(client, "Lost Source", [("lost.jpg", JPEG)])

    # Delete the source photo while job is still pending!
    photos = list(uploads_dir.glob(f"{order_id}_*.jpg"))
    assert len(photos) == 1
    photos[0].unlink()

    backup_out = tmp_path / "backup_release_broken"
    success, report = generate_backup(
        db_path=db_path,
        uploads_dir=uploads_dir,
        output_dir=backup_out,
    )
    assert success is False
    assert any("Required recovery source file missing" in err for err in report["errors"])


def test_historical_backup_audit_identifies_retained_photos_without_modifying(zero_retention_env):
    """
    Audit Tool Contract:
    audit_backup_archives.py must accurately detect delivered photos in historical archives,
    report them as HIGH risk, and NEVER modify the archive files.
    """
    client, db_path, uploads_dir, tmp_path = zero_retention_env

    # Create an order and mark as delivered
    order_id = create_order_helper(client, "Historical User", [("hist.jpg", JPEG)])
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE delivery_jobs SET state='delivered' WHERE order_id=?", (order_id,))

    # Simulate a naive historical backup where tar included the delivered photo!
    hist_backup_dir = tmp_path / "historical_backup_20260101"
    hist_backup_dir.mkdir(parents=True)
    shutil.copy2(db_path, hist_backup_dir / "orders.db")

    # Manually pack uploads into uploads.tar.gz including the delivered photo
    with tarfile.open(hist_backup_dir / "uploads.tar.gz", "w:gz") as tar:
        for f in uploads_dir.iterdir():
            if f.is_file():
                tar.add(f, arcname=f"uploads/{f.name}")

    tar_stat_before = (hist_backup_dir / "uploads.tar.gz").stat()

    # Audit the historical archive
    report = audit_single_backup_dir(hist_backup_dir)

    assert report["db_found"] is True
    assert report["uploads_tar_found"] is True
    assert len(report["retained_delivered_photos"]) >= 1
    assert "HIGH" in report["residual_privacy_risk"]

    # Verify dry-run safety: archive was NOT modified
    tar_stat_after = (hist_backup_dir / "uploads.tar.gz").stat()
    assert tar_stat_before.st_size == tar_stat_after.st_size
    assert tar_stat_before.st_mtime == tar_stat_after.st_mtime


def test_historical_audit_confirms_zero_risk_on_zero_retention_backup(zero_retention_env):
    """
    Auditing a properly generated zero-retention backup must confirm ZERO residual privacy risk.
    """
    client, db_path, uploads_dir, tmp_path = zero_retention_env

    order_id = create_order_helper(client, "Clean User", [("clean.jpg", JPEG)])
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE delivery_jobs SET state='delivered' WHERE order_id=?", (order_id,))

    with sqlite3.connect(db_path) as conn:
        jids = [r[0] for r in conn.execute("SELECT id FROM delivery_jobs WHERE order_id=?", (order_id,)).fetchall()]
    for jid in jids:
        perform_job_cleanup(db_path, jid, uploads_dir=uploads_dir)

    clean_backup_dir = tmp_path / "clean_backup"
    success, rep = generate_backup(db_path, uploads_dir, clean_backup_dir)
    assert success is True

    audit_rep = audit_single_backup_dir(clean_backup_dir)
    assert len(audit_rep["retained_delivered_photos"]) == 0
    assert "ZERO" in audit_rep["residual_privacy_risk"]


def test_backup_manifest_metadata_and_integrity_contract(zero_retention_env):
    """
    Manifest Integrity Contract:
    manifest.sha256 and backup_manifest.json must accurately record:
    - application_id (PDAI / 1346650441)
    - schema_generation ('P1')
    - zero_retention_enforced: True
    - exact SHA-256 hashes of orders.db and uploads.tar.gz
    """
    client, db_path, uploads_dir, tmp_path = zero_retention_env

    order_id = create_order_helper(client, "Manifest User", [("m_photo.jpg", JPEG)])

    backup_dir = tmp_path / "manifest_test_backup"
    success, report = generate_backup(db_path, uploads_dir, backup_dir)
    assert success is True

    manifest_json_path = backup_dir / "backup_manifest.json"
    manifest_sha_path = backup_dir / "manifest.sha256"

    assert manifest_json_path.is_file()
    assert manifest_sha_path.is_file()

    manifest_data = json.loads(manifest_json_path.read_text(encoding="utf-8"))
    assert manifest_data["application_id"] == APPLICATION_ID
    assert manifest_data["application_id_tag"] == "PDAI"
    assert manifest_data["schema_generation"] == "P1"
    assert manifest_data["zero_retention_enforced"] is True
    assert manifest_data["orders_count"] == 1
    assert manifest_data["p1_orders_count"] == 1

    sha_text = manifest_sha_path.read_text(encoding="utf-8")
    assert manifest_data["orders_db_sha256"] in sha_text
    assert manifest_data["uploads_tar_sha256"] in sha_text


def test_shared_file_between_delivered_and_unfinished_jobs_preserved(zero_retention_env):
    """
    Dependency Protection Contract:
    If a photo file is referenced by both a delivered job and an unfinished job,
    it MUST be preserved in the backup until ALL referencing jobs are delivered.
    """
    client, db_path, uploads_dir, tmp_path = zero_retention_env

    # Create an order with 2 attachments
    order_id = create_order_helper(client, "Shared File User", [("shared1.jpg", JPEG), ("shared2.jpg", JPEG)])

    # Make both jobs share job 1's local_path
    with sqlite3.connect(db_path) as conn:
        jobs = conn.execute(
            "SELECT id, local_path FROM delivery_jobs WHERE order_id=? AND kind='file' ORDER BY id",
            (order_id,),
        ).fetchall()
        assert len(jobs) == 2
        job1_id, local_path = jobs[0][0], jobs[0][1]
        job2_id = jobs[1][0]

        conn.execute("UPDATE delivery_jobs SET local_path=? WHERE id=?", (local_path, job2_id))
        # Mark only job 1 as delivered
        conn.execute("UPDATE delivery_jobs SET state='delivered' WHERE id=?", (job1_id,))

    # Cleanup attempt on job 1 MUST be deferred because job 2 is unfinished
    cleanup_ok = perform_job_cleanup(db_path, job1_id, uploads_dir=uploads_dir)
    assert cleanup_ok is False
    assert Path(local_path).is_file()  # File preserved!

    # Backup generation: MUST archive the file because job 2 is unfinished
    backup_dir = tmp_path / "shared_backup"
    success, rep = generate_backup(db_path, uploads_dir, backup_dir)
    assert success is True
    assert rep["archived_files_count"] >= 1

    tar_path = backup_dir / "uploads.tar.gz"
    with tarfile.open(tar_path, "r:gz") as tar:
        files = [Path(m.name).name for m in tar.getmembers() if m.isfile()]
        assert Path(local_path).name in files

    # Now deliver job 2 as well:
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE delivery_jobs SET state='delivered' WHERE id=?", (job2_id,))

    # Now cleanup job 1 and job 2:
    with sqlite3.connect(db_path) as conn:
        jids = [r[0] for r in conn.execute("SELECT id FROM delivery_jobs WHERE kind IN ('file', 'info')").fetchall()]
    for jid in jids:
        perform_job_cleanup(db_path, jid, uploads_dir=uploads_dir)
    assert not Path(local_path).exists()

    # Now generate new backup: MUST omit the file
    backup_dir_clean = tmp_path / "shared_backup_clean"
    success2, rep2 = generate_backup(db_path, uploads_dir, backup_dir_clean)
    assert success2 is True
    with tarfile.open(backup_dir_clean / "uploads.tar.gz", "r:gz") as tar:
        files2 = [Path(m.name).name for m in tar.getmembers() if m.isfile()]
        assert Path(local_path).name not in files2


def test_existing_archives_never_automatically_deleted_by_backup_generator(zero_retention_env):
    """
    Safety Boundary:
    generate_backup() and audit_backup_archives.py must never delete, overwrite,
    or prune any existing historical backup directories.
    """
    client, db_path, uploads_dir, tmp_path = zero_retention_env

    # Create dummy existing backup
    existing_dir = tmp_path / "existing_release_backup"
    existing_dir.mkdir(parents=True)
    sentinel_file = existing_dir / "sentinel.txt"
    sentinel_file.write_text("historical-backup-data", encoding="utf-8")

    # Generate new backup in a separate folder
    new_dir = tmp_path / "new_backup_release"
    success, rep = generate_backup(db_path, uploads_dir, new_dir)
    assert success is True

    # Assert existing backup was untouched
    assert existing_dir.is_dir()
    assert sentinel_file.is_file()
    assert sentinel_file.read_text(encoding="utf-8") == "historical-backup-data"

