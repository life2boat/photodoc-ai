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

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
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
from audit_backup_archives import (  # noqa: E402
    audit_backup_root,
    audit_single_backup_dir,
    audit_single_combined_tar,
    generate_purge_runbook_commands,
    generate_purge_runbook_script,
    get_cli_exit_code,
    main_cli,
    validate_removal_target,
)
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



def test_untracked_photo_in_archive_never_yields_zero(zero_retention_env):
    """
    Fail-Closed Risk Classification Contract:
    An untracked photo inside an archive must NEVER be classified as ZERO residual risk.
    It must yield UNKNOWN and a non-zero exit code (exit code 2).
    """
    client, db_path, uploads_dir, tmp_path = zero_retention_env

    # Valid empty database with no orders
    archive_dir = tmp_path / "archive_with_untracked"
    archive_dir.mkdir(parents=True)
    shutil.copy2(db_path, archive_dir / "orders.db")

    # Tarball contains an untracked photo not referenced by any order
    with tarfile.open(archive_dir / "uploads.tar.gz", "w:gz") as tar:
        untracked = tmp_path / "rogue_customer_photo.jpg"
        untracked.write_bytes(JPEG)
        tar.add(untracked, arcname="uploads/rogue_customer_photo.jpg")

    report = audit_single_backup_dir(archive_dir)
    assert report["db_found"] is True
    assert report["uploads_tar_found"] is True
    assert len(report["untracked_archived_files"]) == 1
    assert "ZERO" not in report["residual_privacy_risk"]
    assert "UNKNOWN" in report["residual_privacy_risk"]
    assert get_cli_exit_code(report["residual_privacy_risk"]) == 2


def test_archive_with_no_delivery_metadata_inconclusive(zero_retention_env):
    """
    Inconclusive Metadata Contract:
    An archive containing files but lacking delivery_jobs metadata
    must return an inconclusive result (UNKNOWN), not ZERO.
    """
    client, db_path, uploads_dir, tmp_path = zero_retention_env

    # Create dummy database missing delivery_jobs table entirely
    dummy_db = tmp_path / "no_meta.db"
    conn = sqlite3.connect(dummy_db)
    conn.execute(f"PRAGMA application_id = {APPLICATION_ID};")
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, filename TEXT);")
    conn.commit()
    conn.close()

    archive_dir = tmp_path / "inconclusive_archive"
    archive_dir.mkdir(parents=True)
    shutil.copy2(dummy_db, archive_dir / "orders.db")

    with tarfile.open(archive_dir / "uploads.tar.gz", "w:gz") as tar:
        photo = tmp_path / "untracked_legacy.jpg"
        photo.write_bytes(JPEG)
        tar.add(photo, arcname="uploads/untracked_legacy.jpg")

    report = audit_single_backup_dir(archive_dir)
    assert "ZERO" not in report["residual_privacy_risk"]
    assert "UNKNOWN" in report["residual_privacy_risk"]
    assert get_cli_exit_code(report["residual_privacy_risk"]) == 2


def test_missing_backup_root_returns_error_status(tmp_path):
    """
    Safety Boundary:
    A missing backup root directory must return ERROR status and exit code 3, never ZERO.
    """
    nonexistent = tmp_path / "does_not_exist_dir"
    summary = audit_backup_root(nonexistent)
    assert "ERROR" in summary["overall_residual_privacy_risk"]
    assert get_cli_exit_code(summary["overall_residual_privacy_risk"]) == 3


def test_empty_archive_inventory_fails_closed(tmp_path):
    """
    Fail-Closed Inventory Contract:
    An empty backup directory with no valid archives must return BLOCKED, never ZERO.
    """
    empty_root = tmp_path / "empty_backups"
    empty_root.mkdir(parents=True)

    summary = audit_backup_root(empty_root)
    assert summary["total_archives_scanned"] == 0
    assert "ZERO" not in summary["overall_residual_privacy_risk"]
    assert "BLOCKED" in summary["overall_residual_privacy_risk"]
    assert get_cli_exit_code(summary["overall_residual_privacy_risk"]) == 2


def test_unsupported_archive_formats_detected_and_block_zero(zero_retention_env):
    """
    Format Conformance Contract:
    Unsupported archive formats (.zip, .tar.bz2) must be detected and block a ZERO risk verdict.
    """
    client, db_path, uploads_dir, tmp_path = zero_retention_env

    root_dir = tmp_path / "mixed_format_root"
    root_dir.mkdir(parents=True)

    # Valid backup dir
    valid_dir = root_dir / "release_valid"
    valid_dir.mkdir(parents=True)
    shutil.copy2(db_path, valid_dir / "orders.db")
    with tarfile.open(valid_dir / "uploads.tar.gz", "w:gz") as tar:
        pass  # empty archive

    # Also drop an unsupported .zip and .tar.bz2 file
    (root_dir / "mystery_archive.zip").write_bytes(b"PKsynthetic-zip")
    (root_dir / "legacy_bundle.tar.bz2").write_bytes(b"BZhsynthetic-bz2")

    summary = audit_backup_root(root_dir)
    assert len(summary["unsupported_archives"]) == 2
    assert "ZERO" not in summary["overall_residual_privacy_risk"]
    assert "BLOCKED" in summary["overall_residual_privacy_risk"]
    assert get_cli_exit_code(summary["overall_residual_privacy_risk"]) == 2


def test_audit_cli_exit_codes(zero_retention_env, monkeypatch):
    """
    CLI Exit Code Contract:
    - Exit 0: all inspected archives are verified ZERO risk.
    - Exit 1: confirmed HIGH risk.
    - Exit 2: UNKNOWN / BLOCKED.
    - Exit 3: ERROR.
    """
    client, db_path, uploads_dir, tmp_path = zero_retention_env

    # 1. Clean archive -> Exit 0
    clean_dir = tmp_path / "cli_clean"
    clean_dir.mkdir(parents=True)
    shutil.copy2(db_path, clean_dir / "orders.db")
    with tarfile.open(clean_dir / "uploads.tar.gz", "w:gz") as tar:
        pass
    monkeypatch.setattr(sys, "argv", ["audit_backup_archives.py", "--archive-dir", str(clean_dir)])
    assert main_cli() == 0

    # 2. Confirmed delivered photo retained -> Exit 1
    high_dir = tmp_path / "cli_high"
    high_dir.mkdir(parents=True)
    order_id = create_order_helper(client, "High CLI", [("h.jpg", JPEG)])
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE delivery_jobs SET state='delivered' WHERE order_id=?", (order_id,))
    shutil.copy2(db_path, high_dir / "orders.db")
    with tarfile.open(high_dir / "uploads.tar.gz", "w:gz") as tar:
        for f in uploads_dir.iterdir():
            tar.add(f, arcname=f"uploads/{f.name}")
    monkeypatch.setattr(sys, "argv", ["audit_backup_archives.py", "--archive-dir", str(high_dir)])
    assert main_cli() == 1

    # 3. Empty backup root -> Exit 2
    empty_root = tmp_path / "cli_empty"
    empty_root.mkdir(parents=True)
    monkeypatch.setattr(sys, "argv", ["audit_backup_archives.py", "--backup-root", str(empty_root)])
    assert main_cli() == 2

    # 4. Corrupt DB -> Exit 3
    bad_dir = tmp_path / "cli_corrupt"
    bad_dir.mkdir(parents=True)
    (bad_dir / "orders.db").write_bytes(b"corrupted-sqlite-data")
    (bad_dir / "uploads.tar.gz").write_bytes(b"not-a-tar")
    monkeypatch.setattr(sys, "argv", ["audit_backup_archives.py", "--archive-dir", str(bad_dir)])
    assert main_cli() == 3


def test_mixed_legacy_p0_database_fails_closed_on_ambiguous_files(zero_retention_env):
    """
    Legacy P0 Recovery Contract:
    When delivery_jobs exists, physical files for legacy P0 orders without delivery_jobs
    records are ambiguous. The backup generator must fail closed by default,
    and only include/exclude them when the owner explicitly reconciles the policy.
    """
    client, db_path, uploads_dir, tmp_path = zero_retention_env

    # Create a P1 order
    p1_order_id = create_order_helper(client, "P1 User", [("p1.jpg", JPEG)])

    # Insert a legacy P0 order directly into orders table (no order_token_hash, no delivery_jobs)
    p0_order_id = 999
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO orders (id, name, phone, format, paper, crop, filename, status, payment_status, created_at) "
            "VALUES (?, 'Legacy User', '+79991110000', '10x15', 'Glossy', 'Fit', 'legacy_pic.jpg', 'completed', 'paid', '2026-01-01')",
            (p0_order_id,),
        )

    # Place the legacy photo on disk in uploads_dir
    legacy_file = uploads_dir / f"{p0_order_id}_legacy_pic.jpg"
    legacy_file.write_bytes(JPEG)

    # 1. Default policy ("fail"): MUST fail closed with diagnostic
    backup_out_fail = tmp_path / "backup_p0_fail"
    success, report = generate_backup(
        db_path=db_path,
        uploads_dir=uploads_dir,
        output_dir=backup_out_fail,
        legacy_p0_policy="fail",
    )
    assert success is False
    assert any("Ambiguous legacy P0 files detected" in err for err in report["errors"])

    # 2. Explicit owner opt-in ("include"): archives legacy photo for recovery
    backup_out_include = tmp_path / "backup_p0_include"
    success_inc, report_inc = generate_backup(
        db_path=db_path,
        uploads_dir=uploads_dir,
        output_dir=backup_out_include,
        legacy_p0_policy="include",
    )
    assert success_inc is True
    assert report_inc["legacy_p0_policy"] == "include"
    with tarfile.open(backup_out_include / "uploads.tar.gz", "r:gz") as tar:
        archived_names = [Path(m.name).name for m in tar.getmembers() if m.isfile()]
        assert legacy_file.name in archived_names

    # 3. Explicit owner opt-in ("exclude"): omits legacy photo to protect privacy
    backup_out_exclude = tmp_path / "backup_p0_exclude"
    success_exc, report_exc = generate_backup(
        db_path=db_path,
        uploads_dir=uploads_dir,
        output_dir=backup_out_exclude,
        legacy_p0_policy="exclude",
    )
    assert success_exc is True
    assert report_exc["legacy_p0_policy"] == "exclude"
    with tarfile.open(backup_out_exclude / "uploads.tar.gz", "r:gz") as tar:
        archived_names = [Path(m.name).name for m in tar.getmembers() if m.isfile()]
        assert legacy_file.name not in archived_names


def test_purge_remediation_workflow_simulation(zero_retention_env):
    """
    Safe 7-Step Purge Runbook Contract:
    Verifies the documented reconstruction workflow without in-place tar --delete:
    1. Inventory and checksum
    2. Working directory copy
    3. Decompress / extract, remove target delivered file, rebuild compressed tar
    4. Verify readability
    5. Re-run integrity verification and audit (confirm ZERO risk)
    6. Confirm unfinished originals intact
    7. Preserve original until explicit authorization
    """
    client, db_path, uploads_dir, tmp_path = zero_retention_env

    # Order 1: Delivered (delivered photo to purge)
    order1 = create_order_helper(client, "Delivered Order", [("del.jpg", JPEG)])
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE delivery_jobs SET state='delivered' WHERE order_id=?", (order1,))

    # Order 2: Pending (must remain intact!)
    order2 = create_order_helper(client, "Pending Order", [("pend.png", PNG)])

    # Simulate historical archive containing both photos
    hist_backup_dir = tmp_path / "release_to_purge"
    hist_backup_dir.mkdir(parents=True)
    shutil.copy2(db_path, hist_backup_dir / "orders.db")

    tar_path = hist_backup_dir / "uploads.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        for f in uploads_dir.iterdir():
            if f.is_file():
                tar.add(f, arcname=f"uploads/{f.name}")

    tar_stat_original = tar_path.stat()

    # Step 0: Audit confirms HIGH risk
    rep_before = audit_single_backup_dir(hist_backup_dir)
    assert len(rep_before["retained_delivered_photos"]) >= 1
    assert "HIGH" in rep_before["residual_privacy_risk"]

    # --- Execute documented 7-step workflow ---
    # 1. Record original checksum
    import hashlib
    sha_orig = hashlib.sha256(tar_path.read_bytes()).hexdigest()

    # 2. Temporary working directory copy
    work_dir = tmp_path / "purge_work_dir"
    work_dir.mkdir(parents=True)
    shutil.copy2(tar_path, work_dir / "uploads.tar.gz.orig")

    # 3. Rebuild sanitized archive without the approved delivered file
    rebuild_dir = work_dir / "rebuild"
    rebuild_dir.mkdir(parents=True)
    with tarfile.open(work_dir / "uploads.tar.gz.orig", "r:gz") as tar:
        tar.extractall(path=rebuild_dir)

    # Remove delivered customer files (photo and metadata info.txt)
    del_photos = list((rebuild_dir / "uploads").glob(f"{order1}_*.jpg"))
    del_info = list((rebuild_dir / "uploads").glob(f"{order1}_*.txt"))
    assert len(del_photos) >= 1
    for dp in del_photos + del_info:
        dp.unlink()

    sanitized_tar = work_dir / "uploads.tar.gz.sanitized"
    with tarfile.open(sanitized_tar, "w:gz") as tar:
        tar.add(rebuild_dir / "uploads", arcname="uploads")

    # 4. Verify reconstructed archive readability
    with tarfile.open(sanitized_tar, "r:gz") as tar:
        members = [Path(m.name).name for m in tar.getmembers() if m.isfile()]
        assert not any(m.startswith(f"{order1}_") and m.endswith(".jpg") for m in members)
        assert any(m.startswith(f"{order2}_") and m.endswith(".png") for m in members)

    # 5. Run integrity verification and audit on working copy
    shutil.copy2(db_path, work_dir / "orders.db")
    # Audit working directory
    work_report = audit_single_backup_dir(work_dir)
    # Notice: work_dir has uploads.tar.gz.sanitized, not uploads.tar.gz yet
    shutil.copy2(sanitized_tar, work_dir / "uploads.tar.gz")
    work_report = audit_single_backup_dir(work_dir)
    assert len(work_report["retained_delivered_photos"]) == 0
    assert "ZERO" in work_report["residual_privacy_risk"]

    # 6. Confirm unfinished delivery recovery data intact
    assert len(work_report["retained_unfinished_photos"]) >= 1

    # 7. Original archive remained completely untouched during process
    tar_stat_after = tar_path.stat()
    assert tar_stat_original.st_size == tar_stat_after.st_size
    assert hashlib.sha256(tar_path.read_bytes()).hexdigest() == sha_orig


def test_delivered_metadata_alone_produces_high_risk(zero_retention_env):
    """
    Requirement 2:
    An archive retaining delivered info.txt metadata files (even with zero photo bytes)
    must produce HIGH risk and CLI exit code 1.
    """
    client, db_path, uploads_dir, tmp_path = zero_retention_env
    order_id = create_order_helper(client, "Delivered Meta Only", [("photo.jpg", JPEG)])

    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE delivery_jobs SET state='delivered' WHERE order_id=?", (order_id,))

    backup_dir = tmp_path / "delivered_meta_only_dir"
    backup_dir.mkdir(parents=True)
    shutil.copy2(db_path, backup_dir / "orders.db")

    info_files = list(uploads_dir.glob(f"{order_id}_*.txt"))
    assert len(info_files) >= 1

    tar_path = backup_dir / "uploads.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        for inf in info_files:
            tar.add(inf, arcname=f"uploads/{inf.name}")

    report = audit_single_backup_dir(backup_dir)
    assert len(report["retained_delivered_photos"]) == 0
    assert len(report["retained_delivered_info"]) >= 1
    assert "HIGH" in report["residual_privacy_risk"]
    assert "metadata file(s)" in report["residual_privacy_risk"]
    assert get_cli_exit_code(report["residual_privacy_risk"]) == 1


def test_mixed_delivered_photos_and_metadata_produces_high_risk(zero_retention_env):
    """
    Requirement 2:
    Mixed delivered photos and metadata files must produce HIGH risk and itemize both.
    """
    client, db_path, uploads_dir, tmp_path = zero_retention_env
    order_id = create_order_helper(client, "Delivered Mixed", [("photo.jpg", JPEG)])

    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE delivery_jobs SET state='delivered' WHERE order_id=?", (order_id,))

    backup_dir = tmp_path / "delivered_mixed_dir"
    backup_dir.mkdir(parents=True)
    shutil.copy2(db_path, backup_dir / "orders.db")

    tar_path = backup_dir / "uploads.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        for f in uploads_dir.iterdir():
            if f.is_file():
                tar.add(f, arcname=f"uploads/{f.name}")

    report = audit_single_backup_dir(backup_dir)
    assert len(report["retained_delivered_photos"]) >= 1
    assert len(report["retained_delivered_info"]) >= 1
    assert "HIGH" in report["residual_privacy_risk"]
    assert "photo(s)" in report["residual_privacy_risk"]
    assert "metadata file(s)" in report["residual_privacy_risk"]
    assert get_cli_exit_code(report["residual_privacy_risk"]) == 1


def test_multiple_archives_one_risky_yields_high_root_risk(zero_retention_env):
    """
    Requirement 2:
    Multiple archives in backup root where privacy risk exists in only one archive
    must aggregate to overall HIGH risk and report exact counts.
    """
    client, db_path, uploads_dir, tmp_path = zero_retention_env

    # Order 1: Delivered
    order1 = create_order_helper(client, "Order Delivered", [("del.jpg", JPEG)])
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE delivery_jobs SET state='delivered' WHERE order_id=?", (order1,))

    # Order 2: Pending
    order2 = create_order_helper(client, "Order Pending", [("pend.jpg", JPEG)])

    backup_root = tmp_path / "root_multi"
    backup_root.mkdir(parents=True)

    # Archive 1: Clean zero-retention backup (only pending order files)
    arch1_dir = backup_root / "release_clean"
    arch1_dir.mkdir(parents=True)
    shutil.copy2(db_path, arch1_dir / "orders.db")
    with tarfile.open(arch1_dir / "uploads.tar.gz", "w:gz") as tar:
        for f in uploads_dir.glob(f"{order2}_*"):
            tar.add(f, arcname=f"uploads/{f.name}")

    # Archive 2: Historical dirty backup (contains delivered order1 info.txt)
    arch2_dir = backup_root / "release_dirty"
    arch2_dir.mkdir(parents=True)
    shutil.copy2(db_path, arch2_dir / "orders.db")
    with tarfile.open(arch2_dir / "uploads.tar.gz", "w:gz") as tar:
        for f in uploads_dir.glob(f"{order1}_*.txt"):
            tar.add(f, arcname=f"uploads/{f.name}")

    summary = audit_backup_root(backup_root)
    assert summary["total_archives_scanned"] == 2
    assert summary["archives_with_residual_privacy_risk"] == 1
    assert summary["total_retained_delivered_photos"] == 0
    assert summary["total_retained_delivered_info"] >= 1
    assert "HIGH" in summary["overall_residual_privacy_risk"]
    assert get_cli_exit_code(summary["overall_residual_privacy_risk"]) == 1


def test_unfinished_delivery_info_distinguished_from_delivered(zero_retention_env):
    """
    Requirement 2:
    Unfinished delivery originals (photos and info.txt) are recognized as legitimate recovery
    data and evaluate to ZERO risk when no delivered customer data is retained.
    """
    client, db_path, uploads_dir, tmp_path = zero_retention_env
    order_id = create_order_helper(client, "Pending Order", [("pending.jpg", JPEG)])

    backup_dir = tmp_path / "pending_only_dir"
    backup_dir.mkdir(parents=True)
    shutil.copy2(db_path, backup_dir / "orders.db")

    tar_path = backup_dir / "uploads.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        for f in uploads_dir.iterdir():
            if f.is_file():
                tar.add(f, arcname=f"uploads/{f.name}")

    report = audit_single_backup_dir(backup_dir)
    assert len(report["retained_delivered_photos"]) == 0
    assert len(report["retained_delivered_info"]) == 0
    assert len(report["retained_unfinished_photos"]) >= 1
    assert len(report["retained_unfinished_info"]) >= 1
    assert "ZERO" in report["residual_privacy_risk"]
    assert get_cli_exit_code(report["residual_privacy_risk"]) == 0


def test_recursive_discovery_deep_nesting_and_no_duplicate_counting(zero_retention_env):
    """
    Requirement 3:
    Discovers backups nested at least 3 levels deep (e.g. root/lvl1/lvl2/lvl3/dir_backup).
    Avoids double-counting internal uploads.tar.gz as standalone archives.
    """
    client, db_path, uploads_dir, tmp_path = zero_retention_env
    order_id = create_order_helper(client, "Deep Order", [("deep.jpg", JPEG)])

    root_dir = tmp_path / "deep_root"
    deep_dir = root_dir / "year_2026" / "month_10" / "day_28" / "release_01"
    deep_dir.mkdir(parents=True)
    shutil.copy2(db_path, deep_dir / "orders.db")

    with tarfile.open(deep_dir / "uploads.tar.gz", "w:gz") as tar:
        for f in uploads_dir.iterdir():
            if f.is_file():
                tar.add(f, arcname=f"uploads/{f.name}")

    summary = audit_backup_root(root_dir)
    assert summary["total_archives_scanned"] == 1
    assert summary["archives"][0]["archive_type"] == "directory"
    assert "release_01" in summary["archives"][0]["backup_dir"]


def test_standalone_tar_archive_supported(zero_retention_env):
    """
    Requirement 3:
    Standalone uncompressed .tar files are explicitly supported and audited safely.
    """
    client, db_path, uploads_dir, tmp_path = zero_retention_env
    order_id = create_order_helper(client, "Tar Order", [("tar_pic.jpg", JPEG)])

    tar_file = tmp_path / "combined_cutover.tar"
    with tarfile.open(tar_file, "w") as tar:
        tar.add(db_path, arcname="orders.db")
        for f in uploads_dir.iterdir():
            if f.is_file():
                tar.add(f, arcname=f"uploads/{f.name}")

    report = audit_single_combined_tar(tar_file)
    assert report["db_found"] is True
    assert report["total_orders"] >= 1
    assert report["archived_files_total"] >= 1
    assert "ZERO" in report["residual_privacy_risk"]


def test_combined_tar_safe_inspection_path_traversal_rejected(tmp_path):
    """
    Requirement 4:
    Path traversal in combined archive member names (e.g. ../../evil.db) must be rejected
    and fail closed with ERROR without creating files outside temporary directory.
    """
    tar_path = tmp_path / "malicious_traversal.tar"
    with tarfile.open(tar_path, "w") as tar:
        tinfo = tarfile.TarInfo(name="../../evil.db")
        tinfo.size = len(b"fake-db-bytes")
        tinfo.type = tarfile.REGTYPE
        import io
        tar.addfile(tinfo, io.BytesIO(b"fake-db-bytes"))

    report = audit_single_combined_tar(tar_path)
    assert report["residual_privacy_risk"] == "ERROR"
    assert any("Path traversal detected" in err for err in report["errors"])
    assert not (tmp_path.parent / "evil.db").exists()


def test_combined_tar_safe_inspection_duplicate_db_rejected(zero_retention_env):
    """
    Requirement 4:
    Multiple database members in combined archive must fail closed with ERROR.
    """
    _, db_path, uploads_dir, tmp_path = zero_retention_env
    tar_path = tmp_path / "duplicate_db.tar"
    with tarfile.open(tar_path, "w") as tar:
        tar.add(db_path, arcname="orders.db")
        tar.add(db_path, arcname="backup/orders.db")

    report = audit_single_combined_tar(tar_path)
    assert report["residual_privacy_risk"] == "ERROR"
    assert any("Multiple database members detected" in err for err in report["errors"])


def test_combined_tar_safe_inspection_oversized_db_rejected(tmp_path, monkeypatch):
    """
    Requirement 4:
    Database members exceeding resource limits must fail closed with ERROR.
    """
    import audit_backup_archives
    import io
    monkeypatch.setattr(audit_backup_archives, "MAX_DATABASE_MEMBER_SIZE_BYTES", 100)

    tar_path = tmp_path / "oversized.tar"
    with tarfile.open(tar_path, "w") as tar:
        tinfo = tarfile.TarInfo(name="orders.db")
        data = b"x" * 200
        tinfo.size = len(data)
        tinfo.type = tarfile.REGTYPE
        tar.addfile(tinfo, io.BytesIO(data))

    report = audit_single_combined_tar(tar_path)
    assert report["residual_privacy_risk"] == "ERROR"
    assert any("exceeds size limit" in err for err in report["errors"])


def test_combined_tar_safe_inspection_symlink_db_rejected(tmp_path):
    """
    Requirement 4:
    Database members that are symlinks/hardlinks must be rejected.
    """
    tar_path = tmp_path / "symlink_db.tar"
    with tarfile.open(tar_path, "w") as tar:
        tinfo = tarfile.TarInfo(name="orders.db")
        tinfo.type = tarfile.SYMTYPE
        tinfo.linkname = "/etc/passwd"
        tar.addfile(tinfo)

    report = audit_single_combined_tar(tar_path)
    assert report["residual_privacy_risk"] == "ERROR"
    assert any("unsafe symlink/hardlink" in err for err in report["errors"])


def test_purge_runbook_executable_sequence(zero_retention_env):
    """
    Requirement 5:
    Validates the exact 7-step owner-controlled purge runbook:
    1. Inventory and checksum
    2. Isolated working copy (rebuild and candidate subdirectories)
    3. Preserve original (.pre-purge)
    4. Rebuild candidate archive without delivered files
    5. Place candidate under exact expected filenames (candidate/orders.db, candidate/uploads.tar.gz)
    6. Run integrity verifier and audit on candidate copy (all PASS)
    7. Owner authorized replacement
    """
    client, db_path, uploads_dir, tmp_path = zero_retention_env

    order_del = create_order_helper(client, "To Purge", [("delivered.jpg", JPEG)])
    order_pend = create_order_helper(client, "Keep Safe", [("keep.png", PNG)])

    # Simulate historical archive containing both orders BEFORE cleanup
    archive_dir = tmp_path / "hist_release"
    archive_dir.mkdir(parents=True)

    tar_path = archive_dir / "uploads.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        for f in uploads_dir.iterdir():
            if f.is_file():
                tar.add(f, arcname=f"uploads/{f.name}")

    # On server: delivery completes and cleanup is executed with durable evidence
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE delivery_jobs SET state='delivered', delivered_at='2026-10-28T10:00:00+00:00' WHERE order_id=?",
            (order_del,),
        )
        jids = [r[0] for r in conn.execute("SELECT id FROM delivery_jobs WHERE order_id=?", (order_del,)).fetchall()]

    for jid in jids:
        perform_job_cleanup(db_path, jid, uploads_dir=uploads_dir)

    # Store latest DB (reflecting delivered & cleaned state) into historical archive dir
    shutil.copy2(db_path, archive_dir / "orders.db")

    assert "HIGH" in audit_single_backup_dir(archive_dir)["residual_privacy_risk"]

    # STEP 1: Inventory & checksum
    import hashlib
    orig_sha = hashlib.sha256(tar_path.read_bytes()).hexdigest()

    # STEP 2: Isolated working directory
    work_dir = tmp_path / "work_dir"
    rebuild_dir = work_dir / "rebuild"
    candidate_dir = work_dir / "candidate"
    rebuild_dir.mkdir(parents=True)
    candidate_dir.mkdir(parents=True)
    shutil.copy2(tar_path, work_dir / "uploads.tar.gz.orig")
    shutil.copy2(archive_dir / "orders.db", candidate_dir / "orders.db")

    # STEP 3: Preserve original
    shutil.copy2(tar_path, archive_dir / "uploads.tar.gz.pre-purge")

    # STEP 4: Rebuild candidate archive without delivered files
    with tarfile.open(work_dir / "uploads.tar.gz.orig", "r:gz") as tar:
        tar.extractall(path=rebuild_dir)

    for f in list((rebuild_dir / "uploads").glob(f"{order_del}_*")):
        f.unlink()

    candidate_tar = candidate_dir / "uploads.tar.gz"
    with tarfile.open(candidate_tar, "w:gz") as tar:
        tar.add(rebuild_dir / "uploads", arcname="uploads")

    # STEP 5: Exact expected filenames verified
    assert (candidate_dir / "orders.db").is_file()
    assert (candidate_dir / "uploads.tar.gz").is_file()
    assert (rebuild_dir / "uploads").is_dir()

    # STEP 6: Run verification commands on candidate inputs
    valid_integrity, rep_integrity = verify_backup(candidate_dir / "orders.db", rebuild_dir / "uploads")
    assert valid_integrity is True

    audit_candidate = audit_single_backup_dir(candidate_dir)
    assert len(audit_candidate["retained_delivered_photos"]) == 0
    assert len(audit_candidate["retained_delivered_info"]) == 0
    assert "ZERO" in audit_candidate["residual_privacy_risk"]
    assert len(audit_candidate["retained_unfinished_photos"]) >= 1

    # STEP 7: Owner replacement
    shutil.copy2(candidate_tar, archive_dir / "uploads.tar.gz")
    assert "ZERO" in audit_single_backup_dir(archive_dir)["residual_privacy_risk"]


def test_legacy_p0_privacy_metadata_all_policies(zero_retention_env):
    """
    Requirement 6:
    Verifies that backup_manifest.json accurately separates recovery integrity from
    privacy compliance across all three legacy P0 policies (fail, include, exclude).
    """
    _, db_path, uploads_dir, tmp_path = zero_retention_env

    p0_id = 888
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO orders (id, name, phone, format, paper, crop, filename, status, created_at) "
            "VALUES (?, 'Legacy Customer', '+79991118888', '10x15', 'Glossy', 'Fit', 'legacy.jpg', 'completed', datetime('now'));",
            (p0_id,),
        )
    legacy_file = uploads_dir / f"{p0_id}_legacy.jpg"
    legacy_file.write_bytes(JPEG)

    # 1. Policy "fail" (default): fails closed
    fail_dir = tmp_path / "out_p0_fail"
    succ_fail, rep_fail = generate_backup(
        db_path=db_path,
        uploads_dir=uploads_dir,
        output_dir=fail_dir,
        legacy_p0_policy="fail",
    )
    assert succ_fail is False
    assert any("Ambiguous legacy P0 files detected" in e for e in rep_fail["errors"])

    # 2. Policy "include": preserves files for recovery, but zero-retention is NOT enforced!
    inc_dir = tmp_path / "out_p0_inc"
    succ_inc, rep_inc = generate_backup(
        db_path=db_path,
        uploads_dir=uploads_dir,
        output_dir=inc_dir,
        legacy_p0_policy="include",
    )
    assert succ_inc is True
    assert rep_inc["zero_retention_enforced"] is False
    assert "UNKNOWN" in rep_inc["privacy_status"]
    assert rep_inc["legacy_files_recoverable"] is True
    assert rep_inc["recovery_verification_result"] == "PASS"

    manifest_inc = json.loads((inc_dir / "backup_manifest.json").read_text(encoding="utf-8"))
    assert manifest_inc["zero_retention_enforced"] is False
    assert "UNKNOWN" in manifest_inc["privacy_status"]
    assert manifest_inc["legacy_files_recoverable"] is True
    assert manifest_inc["recovery_verification_result"] == "PASS"

    # 3. Policy "exclude": omits files, zero-retention is enforced, not recoverable
    exc_dir = tmp_path / "out_p0_exc"
    succ_exc, rep_exc = generate_backup(
        db_path=db_path,
        uploads_dir=uploads_dir,
        output_dir=exc_dir,
        legacy_p0_policy="exclude",
    )
    assert succ_exc is True
    assert rep_exc["zero_retention_enforced"] is True
    assert "ZERO_RETENTION_EXCLUDED_LEGACY" in rep_exc["privacy_status"]
    assert rep_exc["legacy_files_recoverable"] is False
    assert rep_exc["recovery_verification_result"] == "PASS"

    manifest_exc = json.loads((exc_dir / "backup_manifest.json").read_text(encoding="utf-8"))
    assert manifest_exc["zero_retention_enforced"] is True
    assert "ZERO_RETENTION_EXCLUDED_LEGACY" in manifest_exc["privacy_status"]
    assert manifest_exc["legacy_files_recoverable"] is False
    assert manifest_exc["recovery_verification_result"] == "PASS"


# ==============================================================================
# R4.4.3: Archive Resource Limits, Symlink Boundary Security, & Runbook Execution
# ==============================================================================

def test_uploads_tar_too_many_members_rejected(tmp_path, monkeypatch):
    """
    R4.4.3: Validates that uploads archive exceeding MAX_ARCHIVE_MEMBERS fails closed
    with ERROR_LIMIT_EXCEEDED and residual_privacy_risk=ERROR.
    """
    backup_dir = tmp_path / "backup_dir"
    backup_dir.mkdir()
    db_file = backup_dir / "orders.db"
    with sqlite3.connect(db_file) as conn:
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, filename TEXT, status TEXT);")

    tar_path = backup_dir / "uploads.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        for i in range(4):
            ti = tarfile.TarInfo(name=f"uploads/file_{i}.jpg")
            ti.size = 10
            tar.addfile(ti, io.BytesIO(b"0123456789"))

    monkeypatch.setattr("audit_backup_archives.MAX_ARCHIVE_MEMBERS", 3)

    report = audit_single_backup_dir(backup_dir)
    assert report["residual_privacy_risk"] == "ERROR"
    assert report["privacy_status"] == "ERROR_LIMIT_EXCEEDED"
    assert any("member count limit" in err for err in report["errors"])


def test_combined_tar_too_many_members_rejected(tmp_path, monkeypatch):
    """
    R4.4.3: Validates that combined archive exceeding MAX_ARCHIVE_MEMBERS fails closed
    with ERROR_LIMIT_EXCEEDED and residual_privacy_risk=ERROR.
    """
    tar_path = tmp_path / "combined_backup.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        db_bytes = b"SQLite format 3\x00dummy"
        ti_db = tarfile.TarInfo(name="backup/orders.db")
        ti_db.size = len(db_bytes)
        tar.addfile(ti_db, io.BytesIO(db_bytes))

        for i in range(3):
            ti = tarfile.TarInfo(name=f"backup/uploads/file_{i}.jpg")
            ti.size = 10
            tar.addfile(ti, io.BytesIO(b"0123456789"))

    # Total members = 4. Lower limit to 2 members.
    monkeypatch.setattr("audit_backup_archives.MAX_ARCHIVE_MEMBERS", 2)

    report = audit_single_combined_tar(tar_path)
    assert report["residual_privacy_risk"] == "ERROR"
    assert report["privacy_status"] == "ERROR_LIMIT_EXCEEDED"
    assert any("member count limit" in err for err in report["errors"])


def test_total_uncompressed_size_limit_rejected(tmp_path, monkeypatch):
    """
    R4.4.3: Validates that archive exceeding MAX_TOTAL_UNCOMPRESSED_SIZE_BYTES fails closed.
    """
    backup_dir = tmp_path / "backup_dir"
    backup_dir.mkdir()
    db_file = backup_dir / "orders.db"
    with sqlite3.connect(db_file) as conn:
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, filename TEXT, status TEXT);")

    tar_path = backup_dir / "uploads.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        for i in range(3):
            ti = tarfile.TarInfo(name=f"uploads/file_{i}.jpg")
            ti.size = 20
            tar.addfile(ti, io.BytesIO(b"x" * 20))

    monkeypatch.setattr("audit_backup_archives.MAX_TOTAL_UNCOMPRESSED_SIZE_BYTES", 50)

    report = audit_single_backup_dir(backup_dir)
    assert report["residual_privacy_risk"] == "ERROR"
    assert report["privacy_status"] == "ERROR_LIMIT_EXCEEDED"
    assert any("total uncompressed size" in err for err in report["errors"])


def test_member_exceeding_single_file_limit_rejected(tmp_path, monkeypatch):
    """
    R4.4.3: Validates that single member exceeding MAX_SINGLE_FILE_SIZE_BYTES fails closed.
    """
    backup_dir = tmp_path / "backup_dir"
    backup_dir.mkdir()
    db_file = backup_dir / "orders.db"
    with sqlite3.connect(db_file) as conn:
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, filename TEXT, status TEXT);")

    tar_path = backup_dir / "uploads.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        ti = tarfile.TarInfo(name="uploads/large_photo.jpg")
        ti.size = 100
        tar.addfile(ti, io.BytesIO(b"x" * 100))

    monkeypatch.setattr("audit_backup_archives.MAX_SINGLE_FILE_SIZE_BYTES", 50)

    report = audit_single_backup_dir(backup_dir)
    assert report["residual_privacy_risk"] == "ERROR"
    assert report["privacy_status"] == "ERROR_LIMIT_EXCEEDED"
    assert any("size limit" in err for err in report["errors"])


def test_malformed_negative_member_size_rejected(tmp_path):
    """
    R4.4.3: Validates that member with negative size or corrupt header is rejected with ERROR_CORRUPT.
    """
    backup_dir = tmp_path / "backup_dir"
    backup_dir.mkdir()
    db_file = backup_dir / "orders.db"
    with sqlite3.connect(db_file) as conn:
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, filename TEXT, status TEXT);")

    tar_path = backup_dir / "uploads.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        ti = tarfile.TarInfo(name="uploads/dummy.jpg")
        ti.size = 10
        tar.addfile(ti, io.BytesIO(b"0123456789"))

    orig_open = tarfile.open

    def mock_tarfile_open(*args, **kwargs):
        tf = orig_open(*args, **kwargs)
        orig_getmembers = tf.getmembers

        def mock_getmembers():
            members = orig_getmembers()
            for m in members:
                m.size = -5
            return members

        tf.getmembers = mock_getmembers
        return tf

    with patch("tarfile.open", side_effect=mock_tarfile_open):
        report = audit_single_backup_dir(backup_dir)
        assert report["residual_privacy_risk"] == "ERROR"
        assert report["privacy_status"] == "ERROR_CORRUPT"
        assert any("Malformed member size" in err for err in report["errors"])


def test_database_stream_exceeding_declared_size_rejected(tmp_path, monkeypatch):
    """
    R4.4.3: Validates that database member exceeding MAX_DATABASE_MEMBER_SIZE_BYTES
    in combined archive is rejected with ERROR_LIMIT_EXCEEDED.
    """
    tar_path = tmp_path / "combined_backup.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        db_bytes = b"SQLite format 3\x00" + b"x" * 200
        ti_db = tarfile.TarInfo(name="backup/orders.db")
        ti_db.size = len(db_bytes)
        tar.addfile(ti_db, io.BytesIO(db_bytes))

    monkeypatch.setattr("audit_backup_archives.MAX_DATABASE_MEMBER_SIZE_BYTES", 50)

    report = audit_single_combined_tar(tar_path)
    assert report["residual_privacy_risk"] == "ERROR"
    assert report["privacy_status"] == "ERROR_LIMIT_EXCEEDED"
    assert any("Database member" in err and "size limit" in err for err in report["errors"])


def test_symlinked_directory_pruned_and_blocked(tmp_path, monkeypatch):
    """
    R4.4.3: Validates that symlinked directories in backup root are pruned from traversal,
    recorded in symlinked_entries, and block audit with exit code 2.
    """
    backup_root = tmp_path / "backups"
    backup_root.mkdir()

    valid_dir = backup_root / "valid_backup"
    valid_dir.mkdir()
    with sqlite3.connect(valid_dir / "orders.db") as conn:
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, filename TEXT, status TEXT);")
    with tarfile.open(valid_dir / "uploads.tar.gz", "w:gz") as tar:
        pass

    external_dir = tmp_path / "external_target"
    external_dir.mkdir()
    symlink_dir = backup_root / "symlinked_subdir"

    symlink_created = False
    try:
        symlink_dir.symlink_to(external_dir, target_is_directory=True)
        symlink_created = True
    except OSError:
        symlink_dir.mkdir()

    if not symlink_created:
        orig_is_symlink = Path.is_symlink

        def patched_is_symlink(self):
            if str(self) == str(symlink_dir) or self.name == symlink_dir.name:
                return True
            return orig_is_symlink(self)

        monkeypatch.setattr(Path, "is_symlink", patched_is_symlink)

    summary = audit_backup_root(backup_root)
    assert "BLOCKED" in summary["overall_residual_privacy_risk"]
    assert len(summary["symlinked_entries"]) >= 1
    assert any(str(symlink_dir) in s for s in summary["symlinked_entries"])
    assert get_cli_exit_code(summary) == 2


def test_symlinked_db_file_rejected(tmp_path, monkeypatch):
    """
    R4.4.3: Validates that symlinked database files are detected and cause BLOCKED risk.
    """
    backup_dir = tmp_path / "backup_dir"
    backup_dir.mkdir()
    real_db = tmp_path / "real_orders.db"
    with sqlite3.connect(real_db) as conn:
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, filename TEXT, status TEXT);")
    with tarfile.open(backup_dir / "uploads.tar.gz", "w:gz") as tar:
        pass

    symlink_db = backup_dir / "orders.db"
    symlink_created = False
    try:
        symlink_db.symlink_to(real_db)
        symlink_created = True
    except OSError:
        shutil.copy2(real_db, symlink_db)

    if not symlink_created:
        orig_is_symlink = Path.is_symlink

        def patched_is_symlink(self):
            if str(self) == str(symlink_db) or self.name == symlink_db.name:
                return True
            return orig_is_symlink(self)

        monkeypatch.setattr(Path, "is_symlink", patched_is_symlink)

    report = audit_single_backup_dir(backup_dir)
    assert "BLOCKED" in report["residual_privacy_risk"]
    assert any("Symbolic link detected" in err or "Database file cannot be a symbolic link" in err for err in report["errors"])


def test_symlinked_uploads_tar_rejected(tmp_path, monkeypatch):
    """
    R4.4.3: Validates that symlinked uploads tar archives are detected and cause BLOCKED risk.
    """
    backup_dir = tmp_path / "backup_dir"
    backup_dir.mkdir()
    with sqlite3.connect(backup_dir / "orders.db") as conn:
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, filename TEXT, status TEXT);")
    real_tar = tmp_path / "real_uploads.tar.gz"
    with tarfile.open(real_tar, "w:gz") as tar:
        pass

    symlink_tar = backup_dir / "uploads.tar.gz"
    symlink_created = False
    try:
        symlink_tar.symlink_to(real_tar)
        symlink_created = True
    except OSError:
        shutil.copy2(real_tar, symlink_tar)

    if not symlink_created:
        orig_is_symlink = Path.is_symlink

        def patched_is_symlink(self):
            if str(self) == str(symlink_tar) or self.name == symlink_tar.name:
                return True
            return orig_is_symlink(self)

        monkeypatch.setattr(Path, "is_symlink", patched_is_symlink)

    report = audit_single_backup_dir(backup_dir)
    assert "BLOCKED" in report["residual_privacy_risk"]
    assert any("Symbolic link detected" in err or "Uploads archive cannot be a symbolic link" in err for err in report["errors"])


def test_symlinked_combined_tar_rejected(tmp_path, monkeypatch):
    """
    R4.4.3: Validates that symlinked combined archives are detected and cause BLOCKED risk.
    """
    real_tar = tmp_path / "real_combined.tar.gz"
    with tarfile.open(real_tar, "w:gz") as tar:
        pass

    symlink_tar = tmp_path / "symlink_combined.tar.gz"
    symlink_created = False
    try:
        symlink_tar.symlink_to(real_tar)
        symlink_created = True
    except OSError:
        shutil.copy2(real_tar, symlink_tar)

    if not symlink_created:
        orig_is_symlink = Path.is_symlink

        def patched_is_symlink(self):
            if str(self) == str(symlink_tar) or self.name == symlink_tar.name:
                return True
            return orig_is_symlink(self)

        monkeypatch.setattr(Path, "is_symlink", patched_is_symlink)

    report = audit_single_combined_tar(symlink_tar)
    assert "BLOCKED" in report["residual_privacy_risk"]
    assert any("Combined archive cannot be a symbolic link" in err for err in report["errors"])


def test_symlink_pointing_outside_root_never_inspected(tmp_path, monkeypatch):
    """
    R4.4.3: Validates that a symlink pointing outside the backup root is excluded and blocked,
    and external contents are never visited or audited.
    """
    backup_root = tmp_path / "backups"
    backup_root.mkdir()

    outside_dir = tmp_path / "outside_private"
    outside_dir.mkdir()
    secret_file = outside_dir / "orders.db"
    with sqlite3.connect(secret_file) as conn:
        conn.execute("CREATE TABLE secret_table (data TEXT);")

    symlink_link = backup_root / "escaped_link"
    symlink_created = False
    try:
        symlink_link.symlink_to(outside_dir, target_is_directory=True)
        symlink_created = True
    except OSError:
        symlink_link.mkdir()

    if not symlink_created:
        orig_is_symlink = Path.is_symlink

        def patched_is_symlink(self):
            if str(self) == str(symlink_link) or self.name == symlink_link.name:
                return True
            return orig_is_symlink(self)

        monkeypatch.setattr(Path, "is_symlink", patched_is_symlink)

    summary = audit_backup_root(backup_root)
    assert "BLOCKED" in summary["overall_residual_privacy_risk"]
    assert any(str(symlink_link) in s for s in summary["symlinked_entries"])
    assert summary["total_archives_scanned"] == 0


def get_bash_executable():
    """
    Locates an appropriate bash executable:
    - On Windows, prefers Git Bash ('C:\\Program Files\\Git\\bin\\bash.exe') to avoid WSL path translation issues.
    - On Linux/Unix, uses shutil.which('bash') or '/bin/bash'.
    """
    if sys.platform == "win32":
        for cand in [r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"]:
            if os.path.exists(cand):
                return cand
    bash_path = shutil.which("bash")
    if bash_path:
        return bash_path
    raise RuntimeError("No bash executable found on system")

def can_create_symlinks():
    try:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src")
            dst = os.path.join(td, "dst")
            open(src, "w").close()
            os.symlink(src, dst)
            return os.path.islink(dst)
    except OSError:
        return False



def test_runbook_removal_target_validation_security():
    """
    R4.4.4 Requirement 6:
    Validates that removal targets are strictly confined to the uploads directory:
    - Rejects path traversal (..)
    - Rejects absolute paths (/ or \\)
    - Rejects control characters and command injection payloads
    - Accepts legitimate filenames, spaces, and Unicode
    """
    # 1. Traversal and path separator rejections
    for bad in ["../secret.txt", "/etc/passwd", r"..\boot.ini", r"dir\file.jpg", "sub/file.jpg", "..", "."]:
        with pytest.raises(ValueError, match="path|traversal"):
            validate_removal_target(bad)

    # 2. Control chars and injection payloads
    for injection in [
        "file.jpg; rm -rf /",
        "file.jpg && rm -rf /",
        "file.jpg | cat /etc/passwd",
        "file.jpg $(whoami)",
        "file.jpg `whoami`",
        "file.jpg > /tmp/out",
        "file.jpg < /tmp/in",
        'file.jpg"bad',
        "file.jpg'bad",
        "file\x00.jpg",
        "file\n.jpg",
        "file\r.jpg",
        "",
    ]:
        with pytest.raises(ValueError):
            validate_removal_target(injection)

    # 3. Legitimate filenames
    assert validate_removal_target("normal.jpg") == "normal.jpg"
    assert validate_removal_target("photo with spaces.png") == "photo with spaces.png"
    assert validate_removal_target("заказ_123_фото.jpg") == "заказ_123_фото.jpg"

    # 4. Generator fails closed if any target is invalid
    with pytest.raises(ValueError):
        generate_purge_runbook_script(Path("/tmp"), REPO_DIR, ["normal.jpg", "../traversal.jpg"])


def create_synthetic_archive_fixture(tmp_path):
    """Helper to set up a clean synthetic historical backup archive with 1 delivered and 1 pending order."""
    orig_db_path = getattr(main, "DB_PATH", None)
    orig_upload_dir = getattr(main, "UPLOAD_DIR", None)
    orig_env = {
        k: os.environ.get(k)
        for k in ["ROBOKASSA_MERCHANT_LOGIN", "ROBOKASSA_PASSWORD1", "ROBOKASSA_PASSWORD2", "DELIVERY_WORKER_ENABLED"]
    }
    try:
        db_path = tmp_path / "active_orders.db"
        uploads_dir = tmp_path / "active_uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)

        os.environ["ROBOKASSA_MERCHANT_LOGIN"] = "test-merchant"
        os.environ["ROBOKASSA_PASSWORD1"] = "test-pass-1"
        os.environ["ROBOKASSA_PASSWORD2"] = "test-pass-2"
        os.environ["DELIVERY_WORKER_ENABLED"] = "0"

        main.DB_PATH = db_path
        main.UPLOAD_DIR = uploads_dir
        set_uploads_dir(uploads_dir)
        main.init_db()

        client = TestClient(main.app)

        # Order 1: Delivered (delivered file to be purged)
        resp1 = client.post(
            "/api/order",
            data={"name": "Delivered Customer", "phone": "+79991112233", "format": "10x15"},
            files=[("files", ("del_photo.jpg", JPEG, "image/jpeg"))],
        )
        assert resp1.status_code == 200
        order_del = resp1.json()["order_id"]

        # Order 2: Pending (pending original must be preserved)
        resp2 = client.post(
            "/api/order",
            data={"name": "Pending Customer", "phone": "+79991112233", "format": "10x15"},
            files=[("files", ("safe_photo.png", PNG, "image/png"))],
        )
        assert resp2.status_code == 200
        order_pend = resp2.json()["order_id"]

        archive_dir = tmp_path / "hist_release"
        archive_dir.mkdir(parents=True, exist_ok=True)

        # Package initial uploads into historical uploads.tar.gz
        tar_path = archive_dir / "uploads.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tar:
            for f in uploads_dir.iterdir():
                if f.is_file():
                    tar.add(f, arcname=f"uploads/{f.name}")

        # Complete delivery and cleanup for order 1 on the server
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE delivery_jobs SET state='delivered', delivered_at='2026-10-28T10:00:00+00:00' WHERE order_id=?",
                (order_del,),
            )
            jids = [r[0] for r in conn.execute("SELECT id FROM delivery_jobs WHERE order_id=?", (order_del,)).fetchall()]

        for jid in jids:
            perform_job_cleanup(db_path, jid, uploads_dir=uploads_dir)

        # Copy server database to archive_dir
        shutil.copy2(db_path, archive_dir / "orders.db")

        # Audit confirms baseline is HIGH risk
        baseline_rep = audit_single_backup_dir(archive_dir)
        assert "HIGH" in baseline_rep["residual_privacy_risk"]

        del_targets = [item["filename"] for item in baseline_rep["retained_delivered_photos"] + baseline_rep["retained_delivered_info"]]
        return archive_dir, del_targets
    finally:
        if orig_db_path is not None:
            main.DB_PATH = orig_db_path
        if orig_upload_dir is not None:
            main.UPLOAD_DIR = orig_upload_dir
            set_uploads_dir(orig_upload_dir)
        for k, v in orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_purge_runbook_two_phase_lifecycle_and_owner_authorization(tmp_path):
    """
    R4.4.4 Requirements 1, 2, 4, 5, 11:
    Executes actual generated runbook script through bash in an isolated foreign directory:
    - Phase 1: Preparation & verification passes; original archive remains immutable;
    - Missing authorization: replacement refused, original archive untouched;
    - Explicit valid authorization: atomic replacement succeeds, post-replacement audit confirms ZERO risk.
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)
    tar_path = archive_dir / "uploads.tar.gz"

    orig_bytes = tar_path.read_bytes()
    orig_sha256 = hashlib.sha256(orig_bytes).hexdigest()

    # Generate authoritative runbook script
    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir"
    foreign_dir.mkdir()

    # 1. Run Phase 1: Candidate Preparation & Verification
    proc_prep = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_prep.returncode == 0, f"Phase 1 failed:\nSTDOUT: {proc_prep.stdout}\nSTDERR: {proc_prep.stderr}"
    assert "CANDIDATE PREPARATION & VERIFICATION COMPLETE" in proc_prep.stdout

    # Original archive MUST remain completely unchanged!
    assert tar_path.read_bytes() == orig_bytes
    assert hashlib.sha256(tar_path.read_bytes()).hexdigest() == orig_sha256

    # Preserved baseline MUST exist and match original SHA-256
    pre_purge_file = archive_dir / "uploads.tar.gz.pre-purge"
    assert pre_purge_file.is_file()
    assert pre_purge_file.read_bytes() == orig_bytes
    assert hashlib.sha256(pre_purge_file.read_bytes()).hexdigest() == orig_sha256

    # Candidate workspace metadata
    meta_file = archive_dir / ".purge_staging" / "candidate_metadata.json"
    assert meta_file.is_file()
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    cand_sha = meta["candidate_sha"]
    assert len(cand_sha) == 64

    # 2. Run Phase 2 WITHOUT authorization argument -> must fail closed!
    proc_no_auth = subprocess.run([bash_bin, str(script_path), "--replace-authorized"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_no_auth.returncode == 2
    assert "Owner authorization missing" in proc_no_auth.stderr
    # Archive remains unchanged!
    assert tar_path.read_bytes() == orig_bytes

    # 3. Run Phase 2 WITH valid authorization argument -> replacement succeeds!
    proc_auth = subprocess.run([bash_bin, str(script_path), "--replace-authorized", cand_sha], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_auth.returncode == 0, f"Phase 2 failed:\nSTDOUT: {proc_auth.stdout}\nSTDERR: {proc_auth.stderr}"
    assert "OWNER-AUTHORIZED REPLACEMENT COMPLETED SUCCESSFULLY" in proc_auth.stdout

    # Original archive has been replaced atomically with candidate
    new_bytes = tar_path.read_bytes()
    assert new_bytes != orig_bytes
    assert hashlib.sha256(new_bytes).hexdigest() == cand_sha

    # Preserved baseline remains completely untouched!
    assert pre_purge_file.read_bytes() == orig_bytes

    # Post-replacement audit passes with ZERO risk
    post_audit = audit_single_backup_dir(archive_dir)
    assert "ZERO" in post_audit["residual_privacy_risk"]
    assert len(post_audit["retained_delivered_photos"]) == 0
    assert len(post_audit["retained_delivered_info"]) == 0


def test_purge_runbook_pre_purge_baseline_protection_and_collision(tmp_path):
    """
    R4.4.4 Requirement 3 & Scenarios 4 & 5:
    - Pre-existing .pre-purge destination aborts execution immediately;
    - Existing baseline remains byte-for-byte unchanged;
    - Original archive remains byte-for-byte unchanged.
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)
    tar_path = archive_dir / "uploads.tar.gz"

    orig_bytes = tar_path.read_bytes()

    # Pre-create artificial baseline
    pre_purge_file = archive_dir / "uploads.tar.gz.pre-purge"
    existing_content = b"unrelated-existing-historical-pre-purge-baseline"
    pre_purge_file.write_bytes(existing_content)

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook_collision.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir_coll"
    foreign_dir.mkdir()

    # Execute preparation -> MUST abort because .pre-purge exists!
    proc = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc.returncode != 0
    assert "uploads.tar.gz.pre-purge already exists" in proc.stderr

    # Both files MUST remain byte-for-byte unchanged!
    assert pre_purge_file.read_bytes() == existing_content
    assert tar_path.read_bytes() == orig_bytes


def test_purge_runbook_original_immutability_guard(tmp_path):
    """
    R4.4.4 Requirement 4 & Scenario 6:
    If original archive is modified after Phase 1 inventory/preparation,
    Phase 2 refuses replacement and aborts safely.
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)
    tar_path = archive_dir / "uploads.tar.gz"

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook_guard.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir_guard"
    foreign_dir.mkdir()

    # Phase 1 passes
    proc_prep = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_prep.returncode == 0

    cand_sha = json.loads((archive_dir / ".purge_staging" / "candidate_metadata.json").read_text())["candidate_sha"]

    # Tamper with original archive before Phase 2 replacement
    tampered_bytes = tar_path.read_bytes() + b"tampered-corruption"
    tar_path.write_bytes(tampered_bytes)

    # Phase 2 must refuse replacement!
    proc_repl = subprocess.run([bash_bin, str(script_path), "--replace-authorized", cand_sha], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_repl.returncode != 0
    assert "Original archive was modified after preparation" in proc_repl.stderr

    # Archive is NOT replaced with candidate
    assert tar_path.read_bytes() == tampered_bytes


def test_purge_runbook_failed_verification_prevents_replacement(tmp_path):
    """
    R4.4.4 Scenarios 7, 8, 9:
    - Candidate checksum mismatch prevents replacement;
    - Invalid candidate / verification failure halts execution before replacement.
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)
    tar_path = archive_dir / "uploads.tar.gz"
    orig_bytes = tar_path.read_bytes()

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook_fail.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir_fail"
    foreign_dir.mkdir()

    # Phase 1 succeeds
    proc_prep = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_prep.returncode == 0

    # Scenario 9: Candidate checksum mismatch
    wrong_sha = "0000000000000000000000000000000000000000000000000000000000000000"
    proc_mismatch = subprocess.run([bash_bin, str(script_path), "--replace-authorized", wrong_sha], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_mismatch.returncode != 0
    assert "Authorization SHA-256 mismatch" in proc_mismatch.stderr
    assert tar_path.read_bytes() == orig_bytes


def test_purge_runbook_paths_with_spaces_and_special_chars(tmp_path):
    """
    R4.4.4 Scenario 12:
    Paths and target filenames containing spaces and Unicode characters
    are correctly quoted and execute cleanly end-to-end.
    """
    bash_bin = get_bash_executable()
    space_base = tmp_path / "archive dir with spaces"
    space_base.mkdir(parents=True)

    archive_dir, del_targets = create_synthetic_archive_fixture(space_base)
    tar_path = archive_dir / "uploads.tar.gz"

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = space_base / "runbook_spaces.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign space dir"
    foreign_dir.mkdir(parents=True)

    # Phase 1: prepare
    proc_prep = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_prep.returncode == 0, f"STDOUT: {proc_prep.stdout}\nSTDERR: {proc_prep.stderr}"

    cand_sha = json.loads((archive_dir / ".purge_staging" / "candidate_metadata.json").read_text(encoding="utf-8"))["candidate_sha"]

    # Phase 2: replace
    proc_repl = subprocess.run([bash_bin, str(script_path), "--replace-authorized", cand_sha], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_repl.returncode == 0, f"STDOUT: {proc_repl.stdout}\nSTDERR: {proc_repl.stderr}"
    assert hashlib.sha256(tar_path.read_bytes()).hexdigest() == cand_sha


def test_purge_runbook_replacement_failure_preserves_recovery_artifacts(tmp_path):
    """
    R4.4.4 Scenario 10:
    If replacement encounters an error, the staging recovery artifacts
    are kept intact for investigation and never automatically deleted.
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook_recovery.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir_rec"
    foreign_dir.mkdir()

    proc_prep = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_prep.returncode == 0

    staging_dir = archive_dir / ".purge_staging"
    assert staging_dir.is_dir()
    assert (staging_dir / "candidate" / "uploads.tar.gz").is_file()

    # Corrupt candidate metadata so Phase 2 fails during verification
    (staging_dir / "candidate" / "orders.db").unlink()

    cand_sha = json.loads((staging_dir / "candidate_metadata.json").read_text())["candidate_sha"]
    proc_fail = subprocess.run([bash_bin, str(script_path), "--replace-authorized", cand_sha], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_fail.returncode != 0

    # Staging artifacts MUST still exist for recovery!
    assert staging_dir.is_dir()
    assert (staging_dir / "candidate" / "uploads.tar.gz").is_file()


def test_purge_runbook_existing_staging_artifacts_preserved(tmp_path):
    """
    R4.4.5 Requirement 2:
    Existing .purge_staging directory is never silently deleted.
    Preparation aborts without touching pre-existing recovery artifacts.
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)
    tar_path = archive_dir / "uploads.tar.gz"
    orig_bytes = tar_path.read_bytes()

    # Pre-populate staging directory with synthetic recovery evidence
    staging_dir = archive_dir / ".purge_staging"
    staging_dir.mkdir(parents=True)
    evidence_file = staging_dir / "recovery_evidence.txt"
    sentinel_content = b"CRITICAL_RECOVERY_EVIDENCE_DO_NOT_DELETE"
    evidence_file.write_bytes(sentinel_content)

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook_staging_guard.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir_staging"
    foreign_dir.mkdir()

    proc = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc.returncode != 0
    assert "already exists; aborting to preserve recovery artifacts" in proc.stderr

    # Assert evidence is completely untouched!
    assert evidence_file.is_file()
    assert evidence_file.read_bytes() == sentinel_content
    # Original archive untouched!
    assert tar_path.read_bytes() == orig_bytes


def test_purge_runbook_concurrent_remediation_serialized(tmp_path):
    """
    R4.4.5 Requirement 4:
    Remediation transaction uses an exclusive cross-process lock.
    Competing invocations abort without touching or corrupting artifacts.
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)
    tar_path = archive_dir / "uploads.tar.gz"
    orig_bytes = tar_path.read_bytes()

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook_lock.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir_lock"
    foreign_dir.mkdir()

    # Artificially acquire transaction lock
    lock_dir = archive_dir / ".remediation.lock"
    lock_dir.mkdir()
    (lock_dir / "pid").write_text("999999", encoding="utf-8")

    # Invocation MUST fail closed immediately!
    proc = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc.returncode != 0
    assert "Remediation transaction lock is held by another process" in proc.stderr

    # Original archive remains unchanged
    assert tar_path.read_bytes() == orig_bytes

    # Release lock manually and verify preparation can now succeed
    lock_dir.rmdir() if not any(lock_dir.iterdir()) else shutil.rmtree(lock_dir)

    proc_ok = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_ok.returncode == 0


def test_purge_runbook_temporary_replacement_file_or_symlink_rejected(tmp_path):
    """
    R4.4.5 Requirement 3:
    Rejects predictable temporary replacement path if pre-existing or symlinked.
    External sentinel files remain byte-for-byte unchanged.
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)
    tar_path = archive_dir / "uploads.tar.gz"
    orig_bytes = tar_path.read_bytes()

    sentinel_path = tmp_path / "external_sentinel.txt"
    sentinel_bytes = b"EXTERNAL_SENTINEL_CONFIDENTIAL_CONTENT"
    sentinel_path.write_bytes(sentinel_bytes)

    # Place predictable temporary replacement path as regular file or symlink
    tmp_replace = archive_dir / ".uploads.tar.gz.tmp_replacement"
    if can_create_symlinks():
        os.symlink(str(sentinel_path), str(tmp_replace))
    else:
        tmp_replace.write_bytes(b"EXISTING_TMP_FILE_BLOCKER")

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook_symlink_trap.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir_symlink"
    foreign_dir.mkdir()

    proc = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc.returncode != 0
    assert "Predictable replacement path" in proc.stderr or "Path is a symbolic link" in proc.stderr

    # Sentinel file MUST remain 100% byte-for-byte unchanged!
    assert sentinel_path.read_bytes() == sentinel_bytes
    assert tar_path.read_bytes() == orig_bytes


def test_purge_runbook_symlink_archive_dir_rejected(tmp_path):
    """
    R4.4.5 Requirement 3:
    Rejects symlinked archive directory in Python and at runtime.
    Does not use Path.resolve() to convert untrusted symlink.
    """
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)

    if can_create_symlinks():
        symlink_arch = tmp_path / "symlinked_archive"
        os.symlink(str(archive_dir), str(symlink_arch))

        # Python generator rejects symlinked archive directory
        with pytest.raises(ValueError, match="cannot be a symbolic link"):
            generate_purge_runbook_script(symlink_arch, REPO_DIR, del_targets)


def test_purge_runbook_candidate_change_after_prepare_aborts(tmp_path):
    """
    R4.4.5 Scenario 5:
    If candidate archive is modified after Phase 1 preparation,
    Phase 2 confirmation SHA mismatch aborts replacement.
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)
    tar_path = archive_dir / "uploads.tar.gz"
    orig_bytes = tar_path.read_bytes()

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook_cand_tamper.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir_cand"
    foreign_dir.mkdir()

    proc_prep = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_prep.returncode == 0

    cand_sha = json.loads((archive_dir / ".purge_staging" / "candidate_metadata.json").read_text())["candidate_sha"]

    # Tamper with candidate archive in staging
    cand_tar = archive_dir / ".purge_staging" / "candidate" / "uploads.tar.gz"
    cand_tar.write_bytes(cand_tar.read_bytes() + b"corrupt")

    # Phase 2 confirmation must abort!
    proc_conf = subprocess.run([bash_bin, str(script_path), "--confirm", cand_sha], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_conf.returncode != 0
    assert "Confirmation / Authorization SHA-256 mismatch" in proc_conf.stderr

    # Original archive remains unchanged!
    assert tar_path.read_bytes() == orig_bytes


def test_purge_runbook_candidate_integrity_failure_aborts(tmp_path):
    """
    R4.4.5 Scenario 8:
    If candidate database verification fails prior to replacement,
    Phase 2 halts before active archive is modified.
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)
    tar_path = archive_dir / "uploads.tar.gz"
    orig_bytes = tar_path.read_bytes()

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook_integ_fail.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir_integ"
    foreign_dir.mkdir()

    proc_prep = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_prep.returncode == 0

    cand_sha = json.loads((archive_dir / ".purge_staging" / "candidate_metadata.json").read_text())["candidate_sha"]

    # Corrupt candidate DB schema by corrupting file header
    cand_db = archive_dir / ".purge_staging" / "candidate" / "orders.db"
    cand_db.write_bytes(b"NOT_A_SQLITE_DATABASE_HEADER")

    # Re-verify candidate fails, preventing replacement
    proc_conf = subprocess.run([bash_bin, str(script_path), "--confirm", cand_sha], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_conf.returncode != 0

    # Original archive remains 100% unchanged!
    assert tar_path.read_bytes() == orig_bytes


def test_purge_runbook_operator_confirmation_contract(tmp_path):
    """
    R4.4.5 Requirement 6:
    Operator confirmation contract:
    - Missing confirmation argument -> exit code 2
    - Malformed confirmation argument (not 64-hex SHA) -> exit code 2
    - Mismatched confirmation argument -> exit code 1
    In all cases, active archive remains untouched.
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)
    tar_path = archive_dir / "uploads.tar.gz"
    orig_bytes = tar_path.read_bytes()

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook_confirm.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir_conf"
    foreign_dir.mkdir()

    proc_prep = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_prep.returncode == 0

    # 1. Missing argument -> code 2
    res_missing = subprocess.run([bash_bin, str(script_path), "--confirm"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert res_missing.returncode == 2
    assert "Owner authorization missing" in res_missing.stderr or "confirmation value missing" in res_missing.stderr

    # 2. Malformed argument (not 64-hex) -> code 2
    for bad_arg in ["short_hash", "xyz" * 21, "not-hex-chars-1234567890123456789012345678901234567890123456789012"]:
        res_malformed = subprocess.run([bash_bin, str(script_path), "--confirm", bad_arg], cwd=str(foreign_dir), capture_output=True, text=True)
        assert res_malformed.returncode == 2
        assert "malformed or invalid" in res_malformed.stderr

    # 3. Mismatched 64-hex -> code 1
    res_mismatch = subprocess.run([bash_bin, str(script_path), "--confirm", "a" * 64], cwd=str(foreign_dir), capture_output=True, text=True)
    assert res_mismatch.returncode == 1
    assert "Confirmation / Authorization SHA-256 mismatch" in res_mismatch.stderr

    # Archive untouched in all cases
    assert tar_path.read_bytes() == orig_bytes


def test_purge_runbook_post_replacement_failure_triggers_atomic_rollback(tmp_path):
    """
    R4.4.5 Requirement 5 & Scenarios 12, 14:
    If post-replacement verification fails:
    - Safe atomic rollback restores original archive from pre-purge baseline;
    - Preserved baseline is NEVER deleted;
    - Original archive checksum is verified;
    - Recovery artifacts are kept for diagnostics.
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)
    tar_path = archive_dir / "uploads.tar.gz"
    orig_bytes = tar_path.read_bytes()
    orig_sha = hashlib.sha256(orig_bytes).hexdigest()

    # Pre-create valid manifest.sha256 using sha256sum in bash so checksum format matches exactly
    subprocess.run([bash_bin, "-c", "sha256sum orders.db uploads.tar.gz > manifest.sha256"], cwd=str(archive_dir), check=True)

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook_rollback.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir_rollback"
    foreign_dir.mkdir()

    # Phase 1 passes
    proc_prep = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_prep.returncode == 0

    cand_sha = json.loads((archive_dir / ".purge_staging" / "candidate_metadata.json").read_text())["candidate_sha"]

    # Trigger post-replacement verification failure via test hook
    env = {**os.environ, "RUNBOOK_TEST_FAIL_POST_VERIFY": "1"}

    # Run Phase 2 confirmation
    proc_repl = subprocess.run([bash_bin, str(script_path), "--confirm", cand_sha], cwd=str(foreign_dir), capture_output=True, text=True, env=env)
    assert proc_repl.returncode != 0
    assert "CRITICAL: Post-replacement verification or mutation failed! Initiating safe atomic rollback..." in proc_repl.stderr
    assert "ROLLBACK_SUCCESS: Historical archive and manifest safely restored to original baseline" in proc_repl.stderr

    # PROVE: Active archive restored byte-for-byte to original!
    assert tar_path.read_bytes() == orig_bytes
    assert hashlib.sha256(tar_path.read_bytes()).hexdigest() == orig_sha

    # PROVE: Pre-purge baseline remains intact!
    pre_purge_file = archive_dir / "uploads.tar.gz.pre-purge"
    assert pre_purge_file.is_file()
    assert pre_purge_file.read_bytes() == orig_bytes

    # PROVE: Staging artifacts PRESERVED for investigation!
    assert (archive_dir / ".purge_staging").is_dir()
    assert (archive_dir / ".purge_staging" / "candidate" / "uploads.tar.gz").is_file()


def test_purge_runbook_rollback_failure_reports_recovery_required(tmp_path):
    """
    R4.4.5 Scenario 15 & R4.4.6:
    If rollback cannot be completed safely (e.g. restored archive is corrupted),
    emits RECOVERY_REQUIRED and preserves all artifacts and lock.
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook_rec_req.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir_rec_req"
    foreign_dir.mkdir()

    proc_prep = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_prep.returncode == 0

    cand_sha = json.loads((archive_dir / ".purge_staging" / "candidate_metadata.json").read_text())["candidate_sha"]

    # Trigger post-replacement failure AND rollback corruption
    env = {**os.environ, "RUNBOOK_TEST_FAIL_POST_VERIFY": "1", "RUNBOOK_TEST_CORRUPT_ROLLBACK": "1"}

    proc_repl = subprocess.run([bash_bin, str(script_path), "--confirm", cand_sha], cwd=str(foreign_dir), capture_output=True, text=True, env=env)
    assert proc_repl.returncode != 0
    assert "RECOVERY_REQUIRED: Manual intervention required" in proc_repl.stderr
    assert (archive_dir / ".purge_staging").is_dir()
    # Lock is preserved in FAILED state!
    assert (archive_dir / ".remediation.lock").is_dir()


def test_purge_runbook_archive_rename_succeeds_manifest_rename_fails_triggers_rollback(tmp_path):
    """
    R4.4.6 Scenario 1 & 2:
    Archive rename succeeds, but manifest rename fails.
    Script must trigger recovery rollback, restoring active archive and manifest byte-for-byte.
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)
    tar_path = archive_dir / "uploads.tar.gz"
    db_path = archive_dir / "orders.db"
    man_path = archive_dir / "manifest.sha256"

    # Pre-create valid manifest
    subprocess.run([bash_bin, "-c", "sha256sum orders.db uploads.tar.gz > manifest.sha256"], cwd=str(archive_dir), check=True)

    orig_arch_bytes = tar_path.read_bytes()
    orig_arch_sha = hashlib.sha256(orig_arch_bytes).hexdigest()
    orig_db_bytes = db_path.read_bytes()
    orig_db_sha = hashlib.sha256(orig_db_bytes).hexdigest()
    orig_man_bytes = man_path.read_bytes()
    orig_man_sha = hashlib.sha256(orig_man_bytes).hexdigest()

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook_man_fail.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir_man_fail"
    foreign_dir.mkdir()

    proc_prep = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_prep.returncode == 0

    cand_sha = json.loads((archive_dir / ".purge_staging" / "candidate_metadata.json").read_text())["candidate_sha"]

    # Inject failure on manifest rename
    env = {**os.environ, "RUNBOOK_TEST_FAIL_MANIFEST_RENAME": "1"}
    proc_repl = subprocess.run([bash_bin, str(script_path), "--confirm", cand_sha], cwd=str(foreign_dir), capture_output=True, text=True, env=env)
    assert proc_repl.returncode != 0
    assert "Manifest replacement rename failed after archive replacement" in proc_repl.stderr
    assert "ROLLBACK_SUCCESS: Historical archive and manifest safely restored to original baseline" in proc_repl.stderr

    # PROVE: Active files restored byte-for-byte!
    assert tar_path.read_bytes() == orig_arch_bytes
    assert hashlib.sha256(tar_path.read_bytes()).hexdigest() == orig_arch_sha
    assert man_path.read_bytes() == orig_man_bytes
    assert hashlib.sha256(man_path.read_bytes()).hexdigest() == orig_man_sha
    assert db_path.read_bytes() == orig_db_bytes
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == orig_db_sha

    # Recovery artifacts preserved
    assert (archive_dir / ".purge_staging").is_dir()


def test_purge_runbook_rollback_when_manifest_absent_restores_absent_state(tmp_path):
    """
    R4.4.6 Scenario 3:
    When original archive has no manifest, rollback after post-mutation failure
    must remove any newly introduced manifest and verify absence.
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)
    tar_path = archive_dir / "uploads.tar.gz"
    db_path = archive_dir / "orders.db"
    man_path = archive_dir / "manifest.sha256"

    # Ensure NO manifest exists initially
    if man_path.exists():
        man_path.unlink()

    orig_arch_bytes = tar_path.read_bytes()
    orig_arch_sha = hashlib.sha256(orig_arch_bytes).hexdigest()
    orig_db_bytes = db_path.read_bytes()

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook_no_man.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir_no_man"
    foreign_dir.mkdir()

    proc_prep = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_prep.returncode == 0

    cand_sha = json.loads((archive_dir / ".purge_staging" / "candidate_metadata.json").read_text())["candidate_sha"]

    # Inject post-replacement verification failure
    env = {**os.environ, "RUNBOOK_TEST_FAIL_POST_VERIFY": "1"}
    proc_repl = subprocess.run([bash_bin, str(script_path), "--confirm", cand_sha], cwd=str(foreign_dir), capture_output=True, text=True, env=env)
    assert proc_repl.returncode != 0
    assert "ROLLBACK_SUCCESS" in proc_repl.stderr

    # PROVE: Active archive restored byte-for-byte
    assert tar_path.read_bytes() == orig_arch_bytes
    assert hashlib.sha256(tar_path.read_bytes()).hexdigest() == orig_arch_sha
    assert db_path.read_bytes() == orig_db_bytes

    # PROVE: Manifest is completely absent!
    assert not man_path.exists()
    assert (archive_dir / ".purge_staging").is_dir()


def test_purge_runbook_rollback_when_manifest_existed_restores_exact_bytes_and_sha(tmp_path):
    """
    R4.4.6 Scenario 4:
    When original archive has a manifest, rollback must restore its exact bytes and SHA-256.
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)
    tar_path = archive_dir / "uploads.tar.gz"
    man_path = archive_dir / "manifest.sha256"

    subprocess.run([bash_bin, "-c", "sha256sum orders.db uploads.tar.gz > manifest.sha256"], cwd=str(archive_dir), check=True)
    orig_man_bytes = man_path.read_bytes()
    orig_man_sha = hashlib.sha256(orig_man_bytes).hexdigest()
    orig_arch_bytes = tar_path.read_bytes()

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook_exist_man.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir_exist_man"
    foreign_dir.mkdir()

    proc_prep = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_prep.returncode == 0

    cand_sha = json.loads((archive_dir / ".purge_staging" / "candidate_metadata.json").read_text())["candidate_sha"]

    env = {**os.environ, "RUNBOOK_TEST_FAIL_POST_VERIFY": "1"}
    proc_repl = subprocess.run([bash_bin, str(script_path), "--confirm", cand_sha], cwd=str(foreign_dir), capture_output=True, text=True, env=env)
    assert proc_repl.returncode != 0
    assert "ROLLBACK_SUCCESS" in proc_repl.stderr

    # PROVE: Manifest restored to exact original bytes and checksum
    assert man_path.exists()
    assert man_path.read_bytes() == orig_man_bytes
    assert hashlib.sha256(man_path.read_bytes()).hexdigest() == orig_man_sha
    assert tar_path.read_bytes() == orig_arch_bytes


def test_purge_runbook_active_db_changed_after_prepare_aborts_before_mutation(tmp_path):
    """
    R4.4.6 Scenario 5:
    Active orders.db changes after Phase 1; Phase 2 refuses before mutation.
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)
    tar_path = archive_dir / "uploads.tar.gz"
    orig_bytes = tar_path.read_bytes()

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook_db_guard.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir_db_guard"
    foreign_dir.mkdir()

    proc_prep = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_prep.returncode == 0

    cand_sha = json.loads((archive_dir / ".purge_staging" / "candidate_metadata.json").read_text())["candidate_sha"]

    # Mutate active orders.db
    with open(archive_dir / "orders.db", "ab") as f:
        f.write(b"EXTRANEOUS_DATA")

    proc_repl = subprocess.run([bash_bin, str(script_path), "--confirm", cand_sha], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_repl.returncode != 0
    assert "Original database was modified after preparation; aborting replacement" in proc_repl.stderr

    # Active archive untouched
    assert tar_path.read_bytes() == orig_bytes


def test_purge_runbook_active_manifest_changed_after_prepare_aborts_before_mutation(tmp_path):
    """
    R4.4.6 Scenario 6:
    Active manifest.sha256 changes or is deleted/created after Phase 1; Phase 2 refuses before mutation.
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)
    tar_path = archive_dir / "uploads.tar.gz"
    orig_bytes = tar_path.read_bytes()

    # Pre-create manifest
    subprocess.run([bash_bin, "-c", "sha256sum orders.db uploads.tar.gz > manifest.sha256"], cwd=str(archive_dir), check=True)

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook_man_guard.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir_man_guard"
    foreign_dir.mkdir()

    proc_prep = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_prep.returncode == 0

    cand_sha = json.loads((archive_dir / ".purge_staging" / "candidate_metadata.json").read_text())["candidate_sha"]

    # Mutate active manifest
    (archive_dir / "manifest.sha256").write_text("corrupted_manifest_content", encoding="utf-8")

    proc_repl = subprocess.run([bash_bin, str(script_path), "--confirm", cand_sha], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_repl.returncode != 0
    assert "Active manifest.sha256 was modified after preparation; aborting replacement" in proc_repl.stderr

    # Active archive untouched
    assert tar_path.read_bytes() == orig_bytes


def test_purge_runbook_candidate_database_changes_after_prepare_aborts(tmp_path):
    """
    R4.4.6 Scenario 7:
    Candidate database changes after Phase 1; Phase 2 refuses before mutation.
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)
    tar_path = archive_dir / "uploads.tar.gz"
    orig_bytes = tar_path.read_bytes()

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook_cand_db.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir_cand_db"
    foreign_dir.mkdir()

    proc_prep = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_prep.returncode == 0

    cand_sha = json.loads((archive_dir / ".purge_staging" / "candidate_metadata.json").read_text())["candidate_sha"]

    # Mutate candidate orders.db in staging
    with open(archive_dir / ".purge_staging" / "candidate" / "orders.db", "ab") as f:
        f.write(b"CORRUPTED_CANDIDATE_DB")

    proc_repl = subprocess.run([bash_bin, str(script_path), "--confirm", cand_sha], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_repl.returncode != 0
    assert "Candidate database was modified after preparation; aborting replacement" in proc_repl.stderr

    # Active archive untouched
    assert tar_path.read_bytes() == orig_bytes


def test_purge_runbook_candidate_manifest_changes_after_prepare_aborts(tmp_path):
    """
    R4.4.6 Scenario 8:
    Candidate manifest changes after Phase 1; Phase 2 refuses before mutation.
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)
    tar_path = archive_dir / "uploads.tar.gz"
    orig_bytes = tar_path.read_bytes()

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook_cand_man.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir_cand_man"
    foreign_dir.mkdir()

    proc_prep = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_prep.returncode == 0

    cand_sha = json.loads((archive_dir / ".purge_staging" / "candidate_metadata.json").read_text())["candidate_sha"]

    # Mutate candidate manifest in staging
    (archive_dir / ".purge_staging" / "candidate" / "manifest.sha256").write_text("corrupted_candidate_manifest", encoding="utf-8")

    proc_repl = subprocess.run([bash_bin, str(script_path), "--confirm", cand_sha], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_repl.returncode != 0
    assert ("Candidate manifest was modified after preparation; aborting replacement" in proc_repl.stderr or
            "Candidate manifest verification failed; aborting replacement" in proc_repl.stderr)

    # Active archive untouched
    assert tar_path.read_bytes() == orig_bytes


def test_purge_runbook_sigint_before_commit_preserves_active_files_and_releases_lock(tmp_path):
    """
    R4.4.6 Scenario 9:
    SIGINT before commit point preserves active files and releases lock cleanly.
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)
    tar_path = archive_dir / "uploads.tar.gz"
    orig_bytes = tar_path.read_bytes()

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook_sigint.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir_sigint"
    foreign_dir.mkdir()

    proc_prep = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_prep.returncode == 0

    cand_sha = json.loads((archive_dir / ".purge_staging" / "candidate_metadata.json").read_text())["candidate_sha"]

    sync_file = tmp_path / "sync_sigint.tmp"
    env = {**os.environ, "RUNBOOK_TEST_SYNC_FILE": str(sync_file.as_posix())}

    log_path = tmp_path / "sigint_proc.log"
    with open(log_path, "w", encoding="utf-8") as log_f:
        proc = subprocess.Popen([bash_bin, str(script_path), "--confirm", cand_sha], cwd=str(foreign_dir), env=env, stdout=log_f, stderr=log_f)

        # Wait for sync file
        import time
        for _ in range(150):
            if sync_file.exists() or proc.poll() is not None:
                break
            time.sleep(0.1)
        assert sync_file.exists(), f"Process exited early or timed out: rc={proc.returncode}, log={log_path.read_text() if log_path.exists() else ''}"

        # Read bash PID from lock file
        pid_file = archive_dir / ".remediation.lock" / "pid"
        assert pid_file.exists()
        bash_pid = pid_file.read_text(encoding="utf-8").strip()

        # Send INT signal to bash process
        subprocess.run([bash_bin, "-c", f"kill -INT {bash_pid}"])
        if sync_file.exists():
            try:
                sync_file.unlink()
            except Exception:
                pass

        proc.wait(timeout=10)

    log_content = log_path.read_text(encoding="utf-8", errors="replace")
    assert proc.returncode != 0
    assert "Received signal INT" in log_content

    # Active files untouched!
    assert tar_path.read_bytes() == orig_bytes
    # Lock cleanly released!
    assert not (archive_dir / ".remediation.lock").exists()


def test_purge_runbook_sigterm_before_commit_preserves_active_files_and_releases_lock(tmp_path):
    """
    R4.4.6 Scenario 10:
    SIGTERM before commit point preserves active files and releases lock cleanly.
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)
    tar_path = archive_dir / "uploads.tar.gz"
    orig_bytes = tar_path.read_bytes()

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook_sigterm.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir_sigterm"
    foreign_dir.mkdir()

    proc_prep = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_prep.returncode == 0

    cand_sha = json.loads((archive_dir / ".purge_staging" / "candidate_metadata.json").read_text())["candidate_sha"]

    sync_file = tmp_path / "sync_sigterm.tmp"
    env = {**os.environ, "RUNBOOK_TEST_SYNC_FILE": str(sync_file.as_posix())}

    log_path = tmp_path / "sigterm_proc.log"
    with open(log_path, "w", encoding="utf-8") as log_f:
        proc = subprocess.Popen([bash_bin, str(script_path), "--confirm", cand_sha], cwd=str(foreign_dir), env=env, stdout=log_f, stderr=log_f)

        # Wait for sync file
        import time
        for _ in range(150):
            if sync_file.exists() or proc.poll() is not None:
                break
            time.sleep(0.1)
        assert sync_file.exists(), f"Process exited early or timed out: rc={proc.returncode}, log={log_path.read_text() if log_path.exists() else ''}"

        # Read bash PID from lock file
        pid_file = archive_dir / ".remediation.lock" / "pid"
        assert pid_file.exists()
        bash_pid = pid_file.read_text(encoding="utf-8").strip()

        # Send TERM signal to bash process
        subprocess.run([bash_bin, "-c", f"kill -TERM {bash_pid}"])
        if sync_file.exists():
            try:
                sync_file.unlink()
            except Exception:
                pass

        proc.wait(timeout=10)

    log_content = log_path.read_text(encoding="utf-8", errors="replace")
    assert proc.returncode != 0
    assert "Received signal TERM" in log_content

    # Active files untouched!
    assert tar_path.read_bytes() == orig_bytes
    # Lock cleanly released!
    assert not (archive_dir / ".remediation.lock").exists()


def test_purge_runbook_simulated_failure_after_first_commit_invokes_recovery(tmp_path):
    """
    R4.4.6 Scenario 11:
    A failure after the first commit mutation invokes recovery rather than bypassing it.
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)
    tar_path = archive_dir / "uploads.tar.gz"
    orig_bytes = tar_path.read_bytes()

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook_first_commit.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir_first_commit"
    foreign_dir.mkdir()

    proc_prep = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_prep.returncode == 0

    cand_sha = json.loads((archive_dir / ".purge_staging" / "candidate_metadata.json").read_text())["candidate_sha"]

    # Fails immediately after archive replacement
    env = {**os.environ, "RUNBOOK_TEST_FAIL_MANIFEST_RENAME": "1"}
    proc_repl = subprocess.run([bash_bin, str(script_path), "--confirm", cand_sha], cwd=str(foreign_dir), capture_output=True, text=True, env=env)
    assert proc_repl.returncode != 0
    assert "CRITICAL: Post-replacement verification or mutation failed! Initiating safe atomic rollback..." in proc_repl.stderr
    assert "ROLLBACK_SUCCESS" in proc_repl.stderr
    assert tar_path.read_bytes() == orig_bytes


def test_purge_runbook_successful_transaction_verifies_final_state(tmp_path):
    """
    R4.4.6 Scenario 13:
    A successful transaction verifies final archive, manifest, and database state,
    and cleans up staging, journal, and lock.
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)
    tar_path = archive_dir / "uploads.tar.gz"
    orig_bytes = tar_path.read_bytes()

    # Pre-create manifest
    subprocess.run([bash_bin, "-c", "sha256sum orders.db uploads.tar.gz > manifest.sha256"], cwd=str(archive_dir), check=True)

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook_success.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir_success"
    foreign_dir.mkdir()

    # Phase 1
    proc_prep = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_prep.returncode == 0

    cand_sha = json.loads((archive_dir / ".purge_staging" / "candidate_metadata.json").read_text())["candidate_sha"]

    # Phase 2
    proc_repl = subprocess.run([bash_bin, str(script_path), "--confirm", cand_sha], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_repl.returncode == 0
    assert "OWNER-AUTHORIZED REPLACEMENT COMPLETED SUCCESSFULLY" in proc_repl.stdout

    # Verify active files
    assert tar_path.read_bytes() != orig_bytes
    assert hashlib.sha256(tar_path.read_bytes()).hexdigest() == cand_sha

    # Verify active manifest verifies against active files
    proc_chk = subprocess.run([bash_bin, "-c", "sha256sum -c manifest.sha256"], cwd=str(archive_dir), capture_output=True, text=True)
    assert proc_chk.returncode == 0

    # Cleaned up
    assert not (archive_dir / ".purge_staging").exists()
    assert not (archive_dir / ".remediation.lock").exists()
    assert not (archive_dir / ".transaction_journal.json").exists()


def test_purge_runbook_no_manifest_and_existing_manifest_full_lifecycle(tmp_path):
    """
    R4.4.6 Scenario 14:
    Verifies full success lifecycle for both no-manifest and existing-manifest cases.
    """
    bash_bin = get_bash_executable()

    # Case A: No manifest initially
    dir_a = tmp_path / "case_a"
    dir_a.mkdir()
    arch_a, targets_a = create_synthetic_archive_fixture(dir_a)
    script_a = generate_purge_runbook_script(arch_a, REPO_DIR, targets_a)
    path_a = dir_a / "runbook_a.sh"
    path_a.write_text(script_a, encoding="utf-8")

    subprocess.run([bash_bin, str(path_a), "--prepare"], cwd=str(dir_a), check=True)
    cand_a_sha = json.loads((arch_a / ".purge_staging" / "candidate_metadata.json").read_text())["candidate_sha"]
    subprocess.run([bash_bin, str(path_a), "--confirm", cand_a_sha], cwd=str(dir_a), check=True)
    # Manifest now exists and is valid
    assert (arch_a / "manifest.sha256").is_file()
    assert subprocess.run([bash_bin, "-c", "sha256sum -c manifest.sha256"], cwd=str(arch_a)).returncode == 0

    # Case B: Manifest exists initially
    dir_b = tmp_path / "case_b"
    dir_b.mkdir()
    arch_b, targets_b = create_synthetic_archive_fixture(dir_b)
    subprocess.run([bash_bin, "-c", "sha256sum orders.db uploads.tar.gz > manifest.sha256"], cwd=str(arch_b), check=True)
    script_b = generate_purge_runbook_script(arch_b, REPO_DIR, targets_b)
    path_b = dir_b / "runbook_b.sh"
    path_b.write_text(script_b, encoding="utf-8")

    subprocess.run([bash_bin, str(path_b), "--prepare"], cwd=str(dir_b), check=True)
    cand_b_sha = json.loads((arch_b / ".purge_staging" / "candidate_metadata.json").read_text())["candidate_sha"]
    subprocess.run([bash_bin, str(path_b), "--confirm", cand_b_sha], cwd=str(dir_b), check=True)
    assert (arch_b / "manifest.sha256").is_file()
    assert subprocess.run([bash_bin, "-c", "sha256sum -c manifest.sha256"], cwd=str(arch_b)).returncode == 0


def test_purge_runbook_injected_mv_wrapper_manifest_rename_failure(tmp_path):
    """
    R4.4.6:
    Uses an injected wrapper executable for MV_BIN that fails specifically
    when renaming to manifest.sha256, proving real subprocess executable injection!
    """
    bash_bin = get_bash_executable()
    archive_dir, del_targets = create_synthetic_archive_fixture(tmp_path)
    tar_path = archive_dir / "uploads.tar.gz"
    orig_bytes = tar_path.read_bytes()

    # Pre-create manifest
    subprocess.run([bash_bin, "-c", "sha256sum orders.db uploads.tar.gz > manifest.sha256"], cwd=str(archive_dir), check=True)

    script_content = generate_purge_runbook_script(archive_dir, REPO_DIR, del_targets)
    script_path = tmp_path / "runbook_wrapper.sh"
    script_path.write_text(script_content, encoding="utf-8")

    foreign_dir = tmp_path / "foreign_workdir_wrapper"
    foreign_dir.mkdir()

    # Create injected mv wrapper script
    # It fails only once when second argument ends with manifest.sha256 and trigger file exists
    trigger_file = tmp_path / "FAIL_TRIGGER"
    trigger_file.touch()

    wrapper_path = tmp_path / "injected_mv.sh"
    wrapper_content = f"""#!/usr/bin/env bash
TARGET="$2"
if echo "${{TARGET}}" | grep -q "manifest.sha256" && [ -f "{trigger_file.as_posix()}" ]; then
    rm -f "{trigger_file.as_posix()}"
    echo "INJECTED_MV: Simulating manifest rename failure" >&2
    exit 1
fi
exec mv "$@"
"""
    wrapper_path.write_text(wrapper_content, encoding="utf-8")

    proc_prep = subprocess.run([bash_bin, str(script_path), "--prepare"], cwd=str(foreign_dir), capture_output=True, text=True)
    assert proc_prep.returncode == 0

    cand_sha = json.loads((archive_dir / ".purge_staging" / "candidate_metadata.json").read_text())["candidate_sha"]

    # Run with MV_BIN set to our wrapper
    env = {**os.environ, "MV_BIN": f"bash {wrapper_path.as_posix()}"}
    proc_repl = subprocess.run([bash_bin, str(script_path), "--confirm", cand_sha], cwd=str(foreign_dir), capture_output=True, text=True, env=env)
    assert proc_repl.returncode != 0
    assert "INJECTED_MV: Simulating manifest rename failure" in proc_repl.stderr
    assert "ROLLBACK_SUCCESS" in proc_repl.stderr

    # Archive and manifest safely restored!
    assert tar_path.read_bytes() == orig_bytes
