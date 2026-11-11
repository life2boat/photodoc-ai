"""
PHOTODOC-R1-FIX-LEGACY-ARTIFACT-ROLE-01
Mandatory regression test suite for legacy artifact role migration & worker fail-closed safety.

Verifies:
1. Original-only order: legacy source file is classified as 'source', never claimed by worker.
2. Original and completed result: source is 'source', operator result is 'result'. Worker claims only result.
3. Legacy record without artifact_role: column is added and backfilled accurately.
4. Ambiguous filenames and metadata: fake results, corrupted paths, or missing order_results become 'quarantine' and are rejected by worker.
5. Multiple artifacts in one order: all source images classified as 'source', none claimed as result.
6. Paid and unpaid orders: paid status does not make sources deliverable; unpaid orders are never claimed.
7. Repeated migration: reconcile is 100% idempotent and self-healing.
8. Worker execution after migration: end-to-end processing, result delivery, and verified source retention.
9. Attempted delivery of a source image: worker ignores sources, cleanup refuses to delete sources.
10. Rollback/restart after migration: atomic transaction rollback preserves DB on error, restarts cleanly.
"""
import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import consents
import delivery
import main
import operator_workflow
from db_contract import APPLICATION_ID
from migrations.reconcile_delivery_schema import reconcile


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated environment for synthetic legacy database migration testing."""
    db_path = tmp_path / "legacy.db"
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads_dir)
    delivery.set_uploads_dir(uploads_dir)
    monkeypatch.setenv("UPLOAD_DIR", str(uploads_dir))
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("PHOTODOC_ALLOW_FRESH_DB", "1")
    monkeypatch.setenv("DELIVERY_WORKER_ENABLED", "0")
    monkeypatch.setenv("RETENTION_WORKER_ENABLED", "0")
    monkeypatch.setenv("YANDEX_DISK_TOKEN", "")
    monkeypatch.setenv("YANDEX_DISK_STAGING_TOKEN", "")
    monkeypatch.setenv("SMTP_PASSWORD", "")
    monkeypatch.setenv("CONSENT_FEATURE_ENABLED", "1")

    # Synthetic legal approval for test orders
    monkeypatch.setitem(consents.DOCUMENTS[consents.PURPOSE_SERVICE], "approved", True)
    monkeypatch.setitem(consents.DOCUMENTS[consents.PURPOSE_PERSONAL_DATA], "approved", True)

    main.init_db()

    return {"db": db_path, "uploads": uploads_dir}


def _strip_artifact_role_column(db_path: Path):
    """Simulates a legacy pre-PR#37 database where artifact_role column did not exist."""
    conn = sqlite3.connect(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(delivery_jobs)").fetchall()}
    if "artifact_role" in cols:
        conn.execute("ALTER TABLE delivery_jobs DROP COLUMN artifact_role")
        conn.commit()
    conn.close()


def _create_synthetic_source(uploads_dir: Path, filename: str, content: bytes = b"synthetic-source-original") -> tuple[Path, str]:
    path = uploads_dir / filename
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest()


def _create_synthetic_result(uploads_dir: Path, filename: str, content: bytes = b"synthetic-processed-result") -> tuple[Path, str]:
    path = uploads_dir / filename
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest()


# ---------------------------------------------------------------------------
# TEST 1: Original-only order
# ---------------------------------------------------------------------------
def test_original_only_order_classified_as_source_and_never_claimed(env):
    """
    In a legacy DB with an order having only an original photo:
    - Migration classifies the photo as 'source'.
    - Worker claim_due() must return None even when the order is paid.
    - Customer original must never become deliverable.
    """
    db, uploads = env["db"], env["uploads"]
    src_file, src_sha = _create_synthetic_source(uploads, "1_source.jpg")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO orders (id, name, phone, email, format, paper, crop, status, payment_status, order_amount, payment_amount, paid_at, created_at) "
        "VALUES (1, 'Customer', '+79991112233', 'c@example.invalid', '3x4', 'matte', 'standard', 'paid', 'paid', '300', '300', ?, ?)",
        (now, now),
    )
    delivery.enqueue(
        conn, order_id=1, kind="file", local_path=str(src_file),
        original_name="source.jpg", remote_path="PhotoDoc_Orders/Заказ_1_Customer/1_source.jpg",
        sha256=src_sha, size=len(src_file.read_bytes())
    )
    conn.commit()
    conn.close()

    _strip_artifact_role_column(db)

    # Run migration
    res = reconcile(db, apply=True, uploads_dir=uploads)
    assert res["applied"] is True

    conn = sqlite3.connect(db)
    job = conn.execute("SELECT artifact_role, local_path FROM delivery_jobs WHERE kind='file'").fetchone()
    conn.close()
    assert job[0] == "source", f"Expected 'source', got {job[0]}"

    # Worker must NOT claim the original
    claimed = delivery.claim_due(db)
    assert claimed is None, f"Original was claimed for delivery: {claimed}"
    assert src_file.is_file(), "Original file was deleted!"


# ---------------------------------------------------------------------------
# TEST 2: Original and completed result
# ---------------------------------------------------------------------------
def test_original_and_completed_result_correctly_separated(env):
    """
    Legacy DB containing an order with an original source AND an operator result in order_results:
    - Source job gets artifact_role='source'.
    - Result job gets artifact_role='result'.
    - Worker claims ONLY the result job, never the source job.
    """
    db, uploads = env["db"], env["uploads"]
    src_file, src_sha = _create_synthetic_source(uploads, "1_source.jpg")
    res_file, res_sha = _create_synthetic_result(uploads, "1_result_final.jpg")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO orders (id, name, phone, email, format, paper, crop, status, payment_status, order_amount, payment_amount, paid_at, created_at) "
        "VALUES (1, 'Customer', '+79991112233', 'c@example.invalid', '3x4', 'matte', 'standard', 'paid', 'paid', '300', '300', ?, ?)",
        (now, now),
    )
    delivery.enqueue(
        conn, order_id=1, kind="file", local_path=str(src_file),
        original_name="source.jpg", remote_path="PhotoDoc_Orders/Заказ_1_Customer/1_source.jpg",
        sha256=src_sha, size=len(src_file.read_bytes())
    )
    delivery.enqueue(
        conn, order_id=1, kind="file", local_path=str(res_file),
        original_name="1_result.bin", remote_path="PhotoDoc_AI/1_result.bin",
        sha256=res_sha, size=len(res_file.read_bytes())
    )
    operator_workflow.initialize(conn)
    conn.execute(
        "INSERT INTO order_results (order_id, path, sha256, size, version, updated_at) VALUES (1, ?, ?, ?, 1, ?)",
        (str(res_file), res_sha, res_file.stat().st_size, now),
    )
    conn.commit()
    conn.close()

    _strip_artifact_role_column(db)

    # Run migration
    res = reconcile(db, apply=True, uploads_dir=uploads)
    assert res["applied"] is True

    conn = sqlite3.connect(db)
    jobs = conn.execute("SELECT local_path, artifact_role FROM delivery_jobs WHERE kind='file' ORDER BY id").fetchall()
    conn.close()

    roles_by_path = {j[0]: j[1] for j in jobs}
    assert roles_by_path[str(src_file)] == "source"
    assert roles_by_path[str(res_file)] == "result"

    # Worker claim must claim ONLY the result
    claimed = delivery.claim_due(db)
    assert claimed is not None
    assert claimed["artifact_role"] == "result"
    assert claimed["local_path"] == str(res_file)
    assert claimed["content_sha256"] == res_sha

    # Next claim returns None (source is not claimed)
    next_claim = delivery.claim_due(db)
    assert next_claim is None


# ---------------------------------------------------------------------------
# TEST 3: Legacy record without artifact_role
# ---------------------------------------------------------------------------
def test_legacy_record_without_artifact_role_backfilled_accurately(env):
    """
    Ensures that when artifact_role column is completely missing,
    ALTER TABLE adds the column and all rows receive authoritative roles.
    """
    db, uploads = env["db"], env["uploads"]
    src_file, src_sha = _create_synthetic_source(uploads, "2_photo.jpg")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO orders (id, name, phone, status, payment_status, created_at) "
        "VALUES (2, 'Bob', '+79992223344', 'paid', 'paid', ?)", (now,)
    )
    delivery.enqueue(
        conn, order_id=2, kind="file", local_path=str(src_file),
        original_name="photo.jpg", remote_path="PhotoDoc_Orders/Заказ_2_Bob/2_photo.jpg",
        sha256=src_sha, size=len(src_file.read_bytes())
    )
    delivery.enqueue(conn, order_id=2, kind="info", local_path=str(uploads / "info.txt"), remote_path="info.txt")
    delivery.enqueue(conn, order_id=2, kind="paid_email")
    conn.commit()
    conn.close()

    _strip_artifact_role_column(db)

    res = reconcile(db, apply=True, uploads_dir=uploads)
    assert res["applied"] is True

    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT kind, artifact_role FROM delivery_jobs ORDER BY id").fetchall()
    conn.close()

    roles = {r[0]: r[1] for r in rows}
    assert roles["file"] == "source"
    assert roles["info"] == "info"
    # paid_email remains non-file (NULL)
    assert roles["paid_email"] is None


# ---------------------------------------------------------------------------
# TEST 4: Ambiguous filenames and metadata
# ---------------------------------------------------------------------------
def test_ambiguous_filenames_and_metadata_fail_closed_as_quarantine(env):
    """
    Ambiguous or conflicting artifacts must be assigned 'quarantine' and rejected by delivery worker:
    Case A: remote_path ends with _result.bin but no order_results row exists.
    Case B: remote_path ends with _result.bin but local content matches the source photo.
    Case C: local_path contains _result_ but remote_path is not _result.bin.
    Case D: local_path or remote_path is empty.
    """
    db, uploads = env["db"], env["uploads"]
    src_file, src_sha = _create_synthetic_source(uploads, "3_source.jpg")
    fake_res, _ = _create_synthetic_result(uploads, "3_fake_result.jpg")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO orders (id, name, phone, status, payment_status, created_at) "
        "VALUES (3, 'Charlie', '+79993334455', 'paid', 'paid', ?)", (now,)
    )
    # Case A: _result.bin without order_results
    delivery.enqueue(
        conn, order_id=3, kind="file", local_path=str(fake_res),
        remote_path="PhotoDoc_AI/3_result.bin"
    )
    # Case B: _result.bin pointing to source file bytes
    delivery.enqueue(
        conn, order_id=3, kind="file", local_path=str(src_file),
        remote_path="PhotoDoc_AI/3_source_as_result.bin", sha256=src_sha
    )
    # Case C: _result_ in local path, but standard remote_path
    delivery.enqueue(
        conn, order_id=3, kind="file", local_path=str(uploads / "3_result_mismatch.jpg"),
        remote_path="PhotoDoc_Orders/3_source.jpg"
    )
    # Case D: Empty paths
    delivery.enqueue(conn, order_id=3, kind="file", local_path="", remote_path="")
    conn.commit()
    conn.close()

    _strip_artifact_role_column(db)

    # Run migration
    res = reconcile(db, apply=True, uploads_dir=uploads)
    assert res["applied"] is True

    conn = sqlite3.connect(db)
    roles = [r[0] for r in conn.execute("SELECT artifact_role FROM delivery_jobs WHERE kind='file'").fetchall()]
    conn.close()

    # ALL ambiguous artifacts must be 'quarantine'
    assert all(r == "quarantine" for r in roles), f"Expected all 'quarantine', got {roles}"

    # Worker must reject all quarantined artifacts
    claimed = delivery.claim_due(db)
    assert claimed is None, f"Quarantined job was claimed: {claimed}"


# ---------------------------------------------------------------------------
# TEST 5: Multiple artifacts in one order
# ---------------------------------------------------------------------------
def test_multiple_artifacts_in_one_order_all_sources_protected(env):
    """
    An order with multiple uploaded source images:
    - All source images must be classified as 'source'.
    - None of them may ever be claimed as a result.
    """
    db, uploads = env["db"], env["uploads"]
    f1, s1 = _create_synthetic_source(uploads, "4_photo1.jpg", b"photo-1-bytes")
    f2, s2 = _create_synthetic_source(uploads, "4_photo2.jpg", b"photo-2-bytes")
    f3, s3 = _create_synthetic_source(uploads, "4_photo3.jpg", b"photo-3-bytes")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO orders (id, name, phone, status, payment_status, created_at) "
        "VALUES (4, 'Multi', '+79994445566', 'paid', 'paid', ?)", (now,)
    )
    delivery.enqueue(conn, order_id=4, kind="file", local_path=str(f1), remote_path="PhotoDoc_Orders/4/1.jpg", sha256=s1)
    delivery.enqueue(conn, order_id=4, kind="file", local_path=str(f2), remote_path="PhotoDoc_Orders/4/2.jpg", sha256=s2)
    delivery.enqueue(conn, order_id=4, kind="file", local_path=str(f3), remote_path="PhotoDoc_Orders/4/3.jpg", sha256=s3)
    conn.commit()
    conn.close()

    _strip_artifact_role_column(db)

    reconcile(db, apply=True, uploads_dir=uploads)

    conn = sqlite3.connect(db)
    jobs = conn.execute("SELECT artifact_role FROM delivery_jobs WHERE kind='file'").fetchall()
    conn.close()

    assert len(jobs) == 3
    assert all(j[0] == "source" for j in jobs)

    claimed = delivery.claim_due(db)
    assert claimed is None


# ---------------------------------------------------------------------------
# TEST 6: Paid and unpaid orders
# ---------------------------------------------------------------------------
def test_paid_and_unpaid_orders_keep_sources_safe(env):
    """
    Tests that payment status does not cause source images to become deliverable:
    - Unpaid order: worker does not claim anything.
    - Paid order: worker still rejects source images.
    """
    db, uploads = env["db"], env["uploads"]
    src_unpaid, sha_u = _create_synthetic_source(uploads, "5_unpaid.jpg")
    src_paid, sha_p = _create_synthetic_source(uploads, "6_paid.jpg")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO orders (id, name, phone, status, payment_status, created_at) "
        "VALUES (5, 'Unpaid', '+79995556677', 'new', 'unpaid', ?)", (now,)
    )
    delivery.enqueue(conn, order_id=5, kind="file", local_path=str(src_unpaid), remote_path="5.jpg", sha256=sha_u)

    conn.execute(
        "INSERT INTO orders (id, name, phone, status, payment_status, created_at) "
        "VALUES (6, 'Paid', '+79996667788', 'paid', 'paid', ?)", (now,)
    )
    delivery.enqueue(conn, order_id=6, kind="file", local_path=str(src_paid), remote_path="6.jpg", sha256=sha_p)
    conn.commit()
    conn.close()

    _strip_artifact_role_column(db)

    reconcile(db, apply=True, uploads_dir=uploads)

    # Neither unpaid nor paid source image is claimable
    claimed = delivery.claim_due(db)
    assert claimed is None


# ---------------------------------------------------------------------------
# TEST 7: Repeated migration (Idempotency) & Self-Healing
# ---------------------------------------------------------------------------
def test_repeated_migration_is_idempotent_and_self_healing(env):
    """
    1. Running reconcile() repeatedly produces identical results without churn.
    2. Simulates an existing database previously migrated with the buggy logic
       where a source job was erroneously given artifact_role='result'.
       Reconciliation must self-heal and correct the role back to 'source'.
    """
    db, uploads = env["db"], env["uploads"]
    src_file, src_sha = _create_synthetic_source(uploads, "7_source.jpg")
    res_file, res_sha = _create_synthetic_result(uploads, "7_result.jpg")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO orders (id, name, phone, status, payment_status, created_at) "
        "VALUES (7, 'SelfHeal', '+79997778899', 'paid', 'paid', ?)", (now,)
    )
    # Simulate BUGGY previous migration: source job was marked 'result'!
    delivery.enqueue(
        conn, order_id=7, kind="file", local_path=str(src_file),
        original_name="source.jpg", remote_path="PhotoDoc_Orders/7/source.jpg",
        sha256=src_sha, artifact_role="result"
    )
    delivery.enqueue(
        conn, order_id=7, kind="file", local_path=str(res_file),
        original_name="7_result.bin", remote_path="PhotoDoc_AI/7_result.bin",
        sha256=res_sha, artifact_role="result"
    )
    operator_workflow.initialize(conn)
    conn.execute(
        "INSERT INTO order_results (order_id, path, sha256, size, version, updated_at) VALUES (7, ?, ?, ?, 1, ?)",
        (str(res_file), res_sha, res_file.stat().st_size, now),
    )
    conn.commit()
    conn.close()

    # Reconcile must detect the misclassified source job and heal it
    res1 = reconcile(db, apply=True, uploads_dir=uploads)
    assert res1["applied"] is True

    conn = sqlite3.connect(db)
    src_role = conn.execute("SELECT artifact_role FROM delivery_jobs WHERE local_path=?", (str(src_file),)).fetchone()[0]
    res_role = conn.execute("SELECT artifact_role FROM delivery_jobs WHERE local_path=?", (str(res_file),)).fetchone()[0]
    conn.close()

    assert src_role == "source", f"Failed to self-heal misclassified source job: {src_role}"
    assert res_role == "result"

    # Second run: must be cleanly idempotent (no migration needed)
    res2 = reconcile(db, apply=True, uploads_dir=uploads)
    assert res2["migration_required"] is False
    assert res2["applied"] is False


# ---------------------------------------------------------------------------
# TEST 8: Worker execution after migration
# ---------------------------------------------------------------------------
def test_worker_execution_after_migration_delivers_result_preserves_source(env):
    """
    Tests full lifecycle after migration:
    - Worker claims result.
    - Result delivery completes.
    - Delivery cleanup unlinks result.
    - Source image remains intact and untouched on disk.
    """
    db, uploads = env["db"], env["uploads"]
    src_file, src_sha = _create_synthetic_source(uploads, "8_source.jpg")
    res_file, res_sha = _create_synthetic_result(uploads, "8_result.jpg")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO orders (id, name, phone, status, payment_status, created_at) "
        "VALUES (8, 'WorkerExec', '+79998889900', 'paid', 'paid', ?)", (now,)
    )
    delivery.enqueue(
        conn, order_id=8, kind="file", local_path=str(src_file),
        original_name="source.jpg", remote_path="PhotoDoc_Orders/8/source.jpg",
        sha256=src_sha, size=src_file.stat().st_size
    )
    delivery.enqueue(
        conn, order_id=8, kind="file", local_path=str(res_file),
        original_name="8_result.bin", remote_path="PhotoDoc_AI/8_result.bin",
        sha256=res_sha, size=res_file.stat().st_size
    )
    operator_workflow.initialize(conn)
    conn.execute(
        "INSERT INTO order_results (order_id, path, sha256, size, version, updated_at) VALUES (8, ?, ?, ?, 1, ?)",
        (str(res_file), res_sha, res_file.stat().st_size, now),
    )
    conn.commit()
    conn.close()

    _strip_artifact_role_column(db)
    reconcile(db, apply=True, uploads_dir=uploads)

    # 1. Claim
    claimed = delivery.claim_due(db)
    assert claimed is not None
    assert claimed["artifact_role"] == "result"
    assert claimed["local_path"] == str(res_file)

    # 2. Finish delivery
    delivery.finish(db, claimed["id"], lease_until=claimed["lease_until"])

    # 3. Perform cleanup on delivered result
    cleaned = delivery.perform_job_cleanup(db, claimed["id"], uploads_dir=uploads)
    assert cleaned is True
    assert not res_file.exists(), "Delivered result file was not cleaned up!"

    # 4. Source image MUST still exist!
    assert src_file.is_file(), "Source image was deleted by delivery worker!"
    assert src_file.read_bytes() == b"synthetic-source-original"


# ---------------------------------------------------------------------------
# TEST 9: Attempted delivery of a source image
# ---------------------------------------------------------------------------
def test_attempted_delivery_of_source_image_rejected(env):
    """
    Adversarial check:
    - If a job has artifact_role='source', claim_due ignores it.
    - If a job is manually set to delivered and cleanup is attempted,
      perform_job_cleanup refuses to delete the file.
    - process_due_cleanups refuses to sweep source jobs.
    """
    db, uploads = env["db"], env["uploads"]
    src_file, src_sha = _create_synthetic_source(uploads, "9_source.jpg")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO orders (id, name, phone, status, payment_status, created_at) "
        "VALUES (9, 'Adversarial', '+79999990011', 'paid', 'paid', ?)", (now,)
    )
    job_id = delivery.enqueue(
        conn, order_id=9, kind="file", local_path=str(src_file),
        original_name="source.jpg", remote_path="PhotoDoc_Orders/9/source.jpg",
        sha256=src_sha, size=src_file.stat().st_size, artifact_role="source"
    )
    conn.commit()
    conn.close()

    # 1. claim_due rejects it
    assert delivery.claim_due(db) is None

    # 2. Manually force state to delivered to simulate compromised state
    conn = sqlite3.connect(db)
    conn.execute("UPDATE delivery_jobs SET state='delivered', delivered_at=? WHERE id=?", (now, job_id))
    conn.commit()
    conn.close()

    # 3. perform_job_cleanup MUST REFUSE to delete source file
    cleanup_ok = delivery.perform_job_cleanup(db, job_id, uploads_dir=uploads)
    assert cleanup_ok is False, "perform_job_cleanup deleted a source image!"
    assert src_file.is_file(), "Source file was deleted!"

    # 4. process_due_cleanups MUST NOT sweep source jobs
    swept = delivery.process_due_cleanups(db, uploads_dir=uploads)
    assert swept == 0, "process_due_cleanups swept a source job!"


# ---------------------------------------------------------------------------
# TEST 10: Rollback / restart after migration
# ---------------------------------------------------------------------------
def test_rollback_restart_after_migration_failure(env):
    """
    Simulates a failure during reconciliation transaction:
    - Atomic rollback prevents corrupted state.
    - Subsequent run cleanly completes migration.
    """
    db, uploads = env["db"], env["uploads"]
    src_file, src_sha = _create_synthetic_source(uploads, "10_source.jpg")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO orders (id, name, phone, status, payment_status, created_at) "
        "VALUES (10, 'Rollback', '+79990001122', 'paid', 'paid', ?)", (now,)
    )
    delivery.enqueue(
        conn, order_id=10, kind="file", local_path=str(src_file),
        remote_path="10_source.jpg", sha256=src_sha, size=src_file.stat().st_size
    )
    conn.commit()
    conn.close()

    _strip_artifact_role_column(db)

    # Install an intentional abort trigger to force an error during reconciliation transaction
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TRIGGER abort_reconcile BEFORE UPDATE ON delivery_jobs "
        "BEGIN SELECT RAISE(ABORT, 'Simulated transaction abort'); END;"
    )
    conn.commit()
    conn.close()

    with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError)):
        reconcile(db, apply=True, uploads_dir=uploads)

    # Column was not committed or transaction rolled back
    # Now remove the abort trigger and verify reconciliation completes cleanly
    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER abort_reconcile")
    conn.commit()
    conn.close()

    res = reconcile(db, apply=True, uploads_dir=uploads)
    assert res["applied"] is True

    conn = sqlite3.connect(db)
    role = conn.execute("SELECT artifact_role FROM delivery_jobs WHERE id=(SELECT id FROM delivery_jobs)").fetchone()[0]
    conn.close()
    assert role == "source"


# ===========================================================================
# INDEPENDENT CODEX AUDIT ADVERSARIAL REGRESSIONS (HIGH-01..04, MEDIUM-01..02)
# ===========================================================================

def test_adversarial_missing_column_never_claims_file_job(env):
    """HIGH-01: When artifact_role column is absent, claim_due fails closed and claims 0 file jobs."""
    db, uploads = env["db"], env["uploads"]
    src_file, src_sha = _create_synthetic_source(uploads, "adv1_src.jpg")
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO orders (id, name, phone, status, payment_status, created_at) "
        "VALUES (101, 'Adv1', '0', 'paid', 'paid', ?)", (now,)
    )
    delivery.enqueue(
        conn, order_id=101, kind="file", local_path=str(src_file),
        remote_path="PhotoDoc_Orders/101_result.bin", sha256=src_sha, size=src_file.stat().st_size
    )
    conn.commit()
    conn.close()

    _strip_artifact_role_column(db)

    seen = []
    delivery.process_due(db, lambda job: seen.append(Path(job["local_path"]).read_bytes()), uploads_dir=uploads)
    assert not seen, "File job was claimed and processed when role column was missing!"
    assert src_file.exists(), "Source file was deleted!"


def test_adversarial_delivered_legacy_original_survives_migration_and_sweep(env):
    """HIGH-02: Delivered legacy original without order_results must quarantine, not result, and survive sweep."""
    db, uploads = env["db"], env["uploads"]
    src_file, src_sha = _create_synthetic_source(uploads, "adv2_src.jpg")
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO orders (id, name, phone, status, payment_status, created_at) "
        "VALUES (102, 'Adv2', '0', 'paid', 'paid', ?)", (now,)
    )
    jid = delivery.enqueue(
        conn, order_id=102, kind="file", local_path=str(src_file),
        remote_path="PhotoDoc_Orders/102_src.jpg", sha256=src_sha, size=src_file.stat().st_size
    )
    conn.execute("UPDATE delivery_jobs SET state='delivered', delivered_at=? WHERE id=?", (now, jid))
    conn.commit()
    conn.close()

    _strip_artifact_role_column(db)

    res = reconcile(db, apply=True, uploads_dir=uploads)
    assert res["applied"] is True

    conn = sqlite3.connect(db)
    role = conn.execute("SELECT artifact_role FROM delivery_jobs WHERE id=?", (jid,)).fetchone()[0]
    conn.close()

    assert role != "result", f"Delivered legacy original was converted to result: {role}"
    cleaned = delivery.process_due_cleanups(db, uploads_dir=uploads)
    assert cleaned == 0
    assert src_file.exists(), "Delivered legacy original was deleted by cleanup sweep!"


def test_adversarial_conflicting_result_hash_quarantines_and_protects(env):
    """HIGH-03: When order_results has conflicting path vs hash, artifact is quarantined and not delivered."""
    db, uploads = env["db"], env["uploads"]
    src_file, src_sha = _create_synthetic_source(uploads, "adv3_src.jpg")
    proc_file, proc_sha = _create_synthetic_result(uploads, "adv3_proc.jpg")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO orders (id, name, phone, status, payment_status, created_at) "
        "VALUES (103, 'Adv3', '0', 'paid', 'paid', ?)", (now,)
    )
    jid = delivery.enqueue(
        conn, order_id=103, kind="file", local_path=str(src_file),
        remote_path="PhotoDoc_AI/103_result.bin", sha256=src_sha, size=src_file.stat().st_size
    )
    operator_workflow.initialize(conn)
    # Conflicting metadata: path points to proc_file, but sha256 matches src_file!
    conn.execute(
        "INSERT INTO order_results (order_id, path, sha256, size, version, updated_at) "
        "VALUES (103, ?, ?, ?, 1, ?)",
        (str(proc_file), src_sha, proc_file.stat().st_size, now),
    )
    conn.commit()
    conn.close()

    reconcile(db, apply=True, uploads_dir=uploads)

    conn = sqlite3.connect(db)
    role = conn.execute("SELECT artifact_role FROM delivery_jobs WHERE id=?", (jid,)).fetchone()[0]
    conn.close()

    assert role != "result", f"Conflicting artifact accepted as result: {role}"
    assert role == "quarantine"

    seen = []
    delivery.process_due(db, lambda job: seen.append(Path(job["local_path"]).read_bytes()), uploads_dir=uploads)
    assert not seen
    assert src_file.exists()


def test_adversarial_source_in_deleting_phase_never_unlinked(env):
    """HIGH-04 part 1: A source file with phase='deleting' is NEVER deleted if file physically exists."""
    db, uploads = env["db"], env["uploads"]
    src_file, src_sha = _create_synthetic_source(uploads, "adv4_src.jpg")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO orders (id, name, phone, status, payment_status, created_at) "
        "VALUES (104, 'Adv4', '0', 'paid', 'paid', ?)", (now,)
    )
    jid = delivery.enqueue(
        conn, order_id=104, kind="file", local_path=str(src_file),
        remote_path="PhotoDoc_Orders/104_src.jpg", sha256=src_sha, size=src_file.stat().st_size,
        artifact_role="source"
    )
    conn.execute(
        "UPDATE delivery_jobs SET state='delivered', cleanup_state='pending', cleanup_phase='deleting', delivered_at=? WHERE id=?",
        (now, jid),
    )
    conn.commit()
    conn.close()

    cleaned = delivery.perform_job_cleanup(db, jid, uploads_dir=uploads)
    assert cleaned is False
    assert src_file.exists(), "Source file in deleting phase was unlinked!"


def test_adversarial_quarantine_not_returned_by_operator_source_download(env):
    """MEDIUM-01: Quarantined artifacts are never returned to operator as legitimate source photos."""
    db, uploads = env["db"], env["uploads"]
    quar_file, quar_sha = _create_synthetic_source(uploads, "adv5_quar.jpg")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO orders (id, name, phone, status, payment_status, created_at) "
        "VALUES (105, 'Adv5', '0', 'paid', 'paid', ?)", (now,)
    )
    delivery.enqueue(
        conn, order_id=105, kind="file", local_path=str(quar_file),
        remote_path="PhotoDoc_AI/105_result.bin", sha256=quar_sha, size=quar_file.stat().st_size,
        artifact_role="quarantine"
    )
    conn.commit()
    conn.close()

    try:
        returned = operator_workflow.source_for_download(db, 105)
        assert returned != quar_file, "Quarantine file returned as source for download!"
    except HTTPException as exc:
        assert exc.status_code in (409, 410)


def test_adversarial_null_role_never_cleaned_directly(env):
    """HIGH-04 part 2: Direct call to perform_job_cleanup refuses to delete NULL-role file."""
    db, uploads = env["db"], env["uploads"]
    src_file, src_sha = _create_synthetic_source(uploads, "adv6_src.jpg")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO orders (id, name, phone, status, payment_status, created_at) "
        "VALUES (106, 'Adv6', '0', 'paid', 'paid', ?)", (now,)
    )
    jid = delivery.enqueue(
        conn, order_id=106, kind="file", local_path=str(src_file),
        remote_path="PhotoDoc_Orders/106_src.jpg", sha256=src_sha, size=src_file.stat().st_size
    )
    conn.execute(
        "UPDATE delivery_jobs SET artifact_role=NULL, state='delivered', cleanup_state='pending', "
        "cleanup_phase='pending', delivered_at=? WHERE id=?",
        (now, jid),
    )
    conn.commit()
    conn.close()

    cleaned = delivery.perform_job_cleanup(db, jid, uploads_dir=uploads)
    assert cleaned is False
    assert src_file.exists(), "NULL-role file was deleted directly by cleanup!"


def test_adversarial_real_delivery_handler_rejects_non_result_artifacts(env, monkeypatch):
    """HIGH-01 handler: main.deliver_job() rejects file jobs whose artifact_role is not result."""
    db, uploads = env["db"], env["uploads"]
    src_file, src_sha = _create_synthetic_source(uploads, "107_adv7_src.jpg")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO orders (id, name, phone, status, payment_status, created_at) "
        "VALUES (107, 'Adv7', '0', 'paid', 'paid', ?)", (now,)
    )
    jid = delivery.enqueue(
        conn, order_id=107, kind="file", local_path=str(src_file),
        remote_path="PhotoDoc_Orders/107_result.bin", sha256=src_sha, size=src_file.stat().st_size
    )
    conn.commit()
    conn.close()

    _strip_artifact_role_column(db)

    sent = []
    def mock_upload(path, *args, **kwargs):
        sent.append(Path(path).read_bytes())
        return True
    monkeypatch.setattr(main, "upload_to_yandex_disk", mock_upload)

    delivery.process_due(db, main.deliver_job, uploads_dir=uploads)
    assert not sent, "Legacy original was sent by real delivery handler!"
    assert src_file.exists(), "Source file was deleted after delivery handler!"


def test_adversarial_repeated_migration_heals_and_protects_delivered_original(env):
    """HIGH-02 repeated: Repeated reconcile() never keeps delivered original as result."""
    db, uploads = env["db"], env["uploads"]
    src_file, src_sha = _create_synthetic_source(uploads, "adv8_src.jpg")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO orders (id, name, phone, status, payment_status, created_at) "
        "VALUES (108, 'Adv8', '0', 'paid', 'paid', ?)", (now,)
    )
    jid = delivery.enqueue(
        conn, order_id=108, kind="file", local_path=str(src_file),
        remote_path="PhotoDoc_Orders/108_src.jpg", sha256=src_sha, size=src_file.stat().st_size
    )
    conn.execute("UPDATE delivery_jobs SET state='delivered', delivered_at=? WHERE id=?", (now, jid))
    conn.commit()
    conn.close()

    _strip_artifact_role_column(db)

    first = reconcile(db, apply=True, uploads_dir=uploads)
    second = reconcile(db, apply=True, uploads_dir=uploads)

    conn = sqlite3.connect(db)
    role = conn.execute("SELECT artifact_role FROM delivery_jobs WHERE id=?", (jid,)).fetchone()[0]
    conn.close()

    assert role != "result"
    assert src_file.exists()


def test_adversarial_finish_rejects_processing_source(env):
    """MEDIUM-02: delivery.finish() returns False and refuses to mark state='delivered' on source jobs."""
    db, uploads = env["db"], env["uploads"]
    src_file, src_sha = _create_synthetic_source(uploads, "adv9_src.jpg")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO orders (id, name, phone, status, payment_status, created_at) "
        "VALUES (109, 'Adv9', '0', 'paid', 'paid', ?)", (now,)
    )
    jid = delivery.enqueue(
        conn, order_id=109, kind="file", local_path=str(src_file),
        remote_path="PhotoDoc_Orders/109_src.jpg", sha256=src_sha, size=src_file.stat().st_size,
        artifact_role="source"
    )
    lease = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    conn.execute("UPDATE delivery_jobs SET state='processing', lease_until=? WHERE id=?", (lease, jid))
    conn.commit()
    conn.close()

    finished = delivery.finish(db, jid, lease_until=lease)
    conn = sqlite3.connect(db)
    state = conn.execute("SELECT state FROM delivery_jobs WHERE id=?", (jid,)).fetchone()[0]
    conn.close()

    assert finished is False
    assert state != "delivered"


# ---------------------------------------------------------------------------
# PHOTODOC-R1-LEGACY-MIGRATION-HIGH-REMEDIATION-03:
# Dedicated Security Acceptance Tests (HIGH-01 / HIGH-02 / HIGH-C1 / HIGH-C2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("protected_role", ["source", "quarantine"])
def test_security_acceptance_a_explicit_protected_role_never_promoted(env, monkeypatch, protected_role):
    """
    HIGH-01 / HIGH-C1:
    Even if order_results contains a matching path & sha256 pointing to the same file,
    an explicit protected role ('source' or 'quarantine') MUST NEVER be promoted to 'result'.
    Attempted delivery must not send the original bytes, and cleanup must not delete the file.
    """
    db, uploads = env["db"], env["uploads"]
    renamed = uploads / "1_result.bin"
    content = b"synthetic-protected-source-content"
    renamed.write_bytes(content)
    now = datetime.now(timezone.utc).isoformat()
    sha = hashlib.sha256(content).hexdigest()

    conn = sqlite3.connect(db)
    operator_workflow.initialize(conn)
    conn.execute(
        "INSERT INTO orders (id, name, phone, email, format, paper, crop, status, payment_status, order_amount, payment_amount, paid_at, created_at) "
        "VALUES (1, 'Cust', '1', 'c@example.invalid', '3x4', 'matte', 'standard', 'paid', 'paid', '300', '300', ?, ?)",
        (now, now)
    )
    conn.execute(
        "INSERT INTO order_results (order_id, path, sha256, size, version, updated_at) VALUES (1, ?, ?, ?, 1, ?)",
        (str(renamed), sha, len(content), now)
    )
    jid = delivery.enqueue(
        conn, order_id=1, kind="file", local_path=str(renamed),
        remote_path="PhotoDoc_Orders/1_result.bin", sha256=sha, size=len(content),
        artifact_role=protected_role
    )
    # Add an unclassified legacy job to trigger reconcile
    other = uploads / "1_other.jpg"
    other.write_bytes(b"other")
    delivery.enqueue(conn, order_id=1, kind="file", local_path=str(other),
                     remote_path="PhotoDoc_Orders/1_other.jpg")
    conn.commit()
    conn.close()

    summary = reconcile(db, apply=True, uploads_dir=uploads)
    assert summary["applied"] is True

    conn = sqlite3.connect(db)
    new_role = conn.execute("SELECT artifact_role FROM delivery_jobs WHERE id=?", (jid,)).fetchone()[0]
    conn.close()

    assert new_role != "result", f"Role was promoted to result: {new_role}"
    assert new_role in (protected_role, "quarantine")

    sent = []
    def mock_upload(path, *args, **kwargs):
        sent.append(Path(path).read_bytes())
        return True
    monkeypatch.setattr(main, "upload_to_yandex_disk", mock_upload)

    delivery.process_due(db, main.deliver_job, uploads_dir=uploads)
    assert content not in sent, "Protected original bytes were sent to uploader!"
    assert renamed.exists(), "Protected original file was deleted!"


def test_security_acceptance_b_stale_claim_revalidated_at_delivery(env, monkeypatch):
    """
    HIGH-02 / HIGH-C2:
    A worker claiming a job before or during migration cannot deliver the file if migration
    reclassified it to 'quarantine'. The worker must re-read authoritative role from SQLite
    before handing bytes to uploader.
    """
    db, uploads = env["db"], env["uploads"]
    original, sha = _create_synthetic_source(uploads, "1_stale_claim.jpg", b"synthetic-stale-claim-original")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO orders (id, name, phone, email, format, paper, crop, status, payment_status, order_amount, payment_amount, paid_at, created_at) "
        "VALUES (2, 'Cust2', '2', 'c@example.invalid', '3x4', 'matte', 'standard', 'paid', 'paid', '300', '300', ?, ?)",
        (now, now)
    )
    jid = delivery.enqueue(
        conn, order_id=2, kind="file", local_path=str(original),
        remote_path="PhotoDoc_Orders/2_result.bin", sha256=sha, size=len(original.read_bytes()),
        artifact_role="result"
    )
    conn.commit()
    conn.close()

    claimed_event, resume_event = threading.Event(), threading.Event()
    sent = []

    def mock_upload(path, *args, **kwargs):
        sent.append(Path(path).read_bytes())
        return True
    monkeypatch.setattr(main, "upload_to_yandex_disk", mock_upload)

    def delayed_delivery(job):
        claimed_event.set()
        assert resume_event.wait(10), "Test timed out waiting to resume delivery worker"
        main.deliver_job(job)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(delivery.process_due, db, delayed_delivery, 1, uploads)
        try:
            assert claimed_event.wait(10), "Test timed out waiting for worker claim"
            # Now run reconcile concurrently - it will reclassify 2_result.bin to quarantine (since no order_results row)
            reconcile(db, apply=True, uploads_dir=uploads)
            conn = sqlite3.connect(db)
            role_after = conn.execute("SELECT artifact_role FROM delivery_jobs WHERE id=?", (jid,)).fetchone()[0]
            conn.close()
            assert role_after == "quarantine"
        finally:
            resume_event.set()

        future.result(timeout=15)

    assert b"synthetic-stale-claim-original" not in sent, "Stale claim delivered original to uploader!"
    assert original.exists(), "Original was deleted after stale claim!"


def test_security_acceptance_c_worker_claim_due_blocked_during_active_migration_gate(env):
    """
    HIGH-02:
    When a migration holds the gate, claim_due() must return None (fail closed).
    """
    db = env["db"]
    holder = delivery.acquire_migration_gate(db, timeout=2.0, lease_seconds=60)
    try:
        assert delivery.is_migration_active(db) is True
        assert delivery.claim_due(db) is None
    finally:
        delivery.release_migration_gate(db, holder)

    assert delivery.is_migration_active(db) is False


def test_security_acceptance_d_worker_cleanup_blocked_during_active_migration_gate(env):
    """
    HIGH-02:
    When a migration holds the gate, perform_job_cleanup() must return False without deleting files.
    """
    db, uploads = env["db"], env["uploads"]
    res_file, res_sha = _create_synthetic_result(uploads, "cleanup_blocked.jpg")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO orders (id, name, phone, status, payment_status, created_at) "
        "VALUES (3, 'Cust3', '3', 'paid', 'paid', ?)", (now,)
    )
    jid = delivery.enqueue(
        conn, order_id=3, kind="file", local_path=str(res_file),
        remote_path="PhotoDoc_Orders/3_result.jpg", sha256=res_sha, size=res_file.stat().st_size,
        artifact_role="result"
    )
    conn.execute("UPDATE delivery_jobs SET state='delivered', delivered_at=? WHERE id=?", (now, jid))
    conn.commit()
    conn.close()

    holder = delivery.acquire_migration_gate(db, timeout=2.0, lease_seconds=60)
    try:
        cleaned = delivery.perform_job_cleanup(db, jid, uploads_dir=uploads)
        assert cleaned is False, "perform_job_cleanup executed while migration gate was active!"
        assert res_file.exists(), "File was deleted while migration gate was active!"
    finally:
        delivery.release_migration_gate(db, holder)


def test_security_acceptance_e_cross_process_mutual_exclusion(env):
    """
    HIGH-02:
    Separate OS process holding migration gate blocks another process from acquiring it.
    The second process times out and fails closed (RuntimeError).
    """
    db = env["db"]
    backend_dir_posix = BACKEND_DIR.as_posix()
    db_posix = db.as_posix()
    code = f"""
import sys, time
sys.path.insert(0, '{backend_dir_posix}')
import delivery
holder = delivery.acquire_migration_gate('{db_posix}', timeout=5.0, lease_seconds=10)
print('ACQUIRED', flush=True)
time.sleep(2)
delivery.release_migration_gate('{db_posix}', holder)
print('RELEASED', flush=True)
"""
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = proc.stdout.readline()
        assert "ACQUIRED" in line, f"Subprocess failed to acquire gate: {proc.stderr.read()}"

        with pytest.raises(RuntimeError, match="timed out"):
            delivery.acquire_migration_gate(db, timeout=0.3, lease_seconds=5)

        proc.wait(timeout=10)
        holder2 = delivery.acquire_migration_gate(db, timeout=5.0, lease_seconds=5)
        assert holder2 is not None
        delivery.release_migration_gate(db, holder2)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_security_acceptance_f_stale_expired_gate_reclaimed_without_deadlock(env):
    """
    HIGH-02:
    If a process crashes while holding gate, the lease expires and a subsequent migration
    safely reclaims the gate without manual intervention or permanent deadlock.
    """
    db = env["db"]
    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    conn = sqlite3.connect(db)
    conn.execute(delivery.GATE_TABLE_SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO schema_migration_gate (gate_name, holder_id, holder_pid, acquired_at, expires_at) "
        "VALUES ('migration', 'dead-holder', 999999, ?, ?)",
        (expired_at, expired_at),
    )
    conn.commit()
    conn.close()

    assert delivery.is_migration_active(db) is False

    holder = delivery.acquire_migration_gate(db, timeout=1.0, lease_seconds=10)
    assert holder is not None
    assert delivery.is_migration_active(db) is True
    delivery.release_migration_gate(db, holder)
    assert delivery.is_migration_active(db) is False


def test_security_acceptance_g_cleanup_rechecks_changed_role_at_unlink(env):
    """
    HIGH-02:
    perform_job_cleanup() performs an authoritative pre-unlink check under BEGIN IMMEDIATE.
    If another transaction or migration changes the role away from 'result' right before unlink,
    cleanup is aborted and the file is untouched.
    """
    db, uploads = env["db"], env["uploads"]
    src_file, src_sha = _create_synthetic_source(uploads, "4_race_unlink.jpg")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO orders (id, name, phone, status, payment_status, created_at) "
        "VALUES (4, 'Cust4', '4', 'paid', 'paid', ?)", (now,)
    )
    jid = delivery.enqueue(
        conn, order_id=4, kind="file", local_path=str(src_file),
        remote_path="PhotoDoc_Orders/4_result.bin", sha256=src_sha, size=src_file.stat().st_size,
        artifact_role="result"
    )
    conn.execute("UPDATE delivery_jobs SET state='delivered', delivered_at=? WHERE id=?", (now, jid))
    conn.commit()
    conn.close()

    def demote_role_hook(_jid, _token):
        c = sqlite3.connect(db)
        c.execute("UPDATE delivery_jobs SET artifact_role='source' WHERE id=?", (jid,))
        c.commit()
        c.close()

    cleaned = delivery.perform_job_cleanup(db, jid, uploads_dir=uploads, pre_unlink_hook=demote_role_hook)
    assert cleaned is False
    assert src_file.exists()
    conn = sqlite3.connect(db)
    role = conn.execute("SELECT artifact_role FROM delivery_jobs WHERE id=?", (jid,)).fetchone()[0]
    conn.close()
    assert role == "source"


def test_security_acceptance_h_process_due_clean_exit_when_migration_active(env, monkeypatch):
    """
    HIGH-02:
    delivery.process_due() returns 0 without calling worker handler when migration is active.
    """
    db, uploads = env["db"], env["uploads"]
    res_file, res_sha = _create_synthetic_result(uploads, "ready_result.jpg")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO orders (id, name, phone, email, format, paper, crop, status, payment_status, order_amount, payment_amount, paid_at, created_at) "
        "VALUES (5, 'Cust5', '5', 'c@example.invalid', '3x4', 'matte', 'standard', 'paid', 'paid', '300', '300', ?, ?)",
        (now, now)
    )
    delivery.enqueue(
        conn, order_id=5, kind="file", local_path=str(res_file),
        remote_path="PhotoDoc_Orders/5_result.jpg", sha256=res_sha, size=res_file.stat().st_size,
        artifact_role="result"
    )
    conn.commit()
    conn.close()

    calls = []
    def handler(job):
        calls.append(job)

    holder = delivery.acquire_migration_gate(db, timeout=2.0, lease_seconds=60)
    try:
        processed = delivery.process_due(db, handler, 10, uploads)
        assert processed == 0
        assert len(calls) == 0
    finally:
        delivery.release_migration_gate(db, holder)

    # After gate release, normal execution works
    processed = delivery.process_due(db, handler, 10, uploads)
    assert processed == 1
    assert len(calls) == 1


def test_security_acceptance_i_unclassified_result_not_offered_as_operator_original(env):
    """
    HIGH-02:
    Operator download must reject unclassified / unknown artifacts, preventing processed
    results or unknown binaries from being served as original source files.
    """
    db, uploads = env["db"], env["uploads"]
    fake_orig = uploads / "6_abcd.jpg"
    fake_orig.write_bytes(b"processed-bytes")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db)
    operator_workflow.initialize(conn)
    conn.execute(
        "INSERT INTO orders (id, name, phone, email, format, paper, crop, status, payment_status, order_amount, payment_amount, paid_at, created_at) "
        "VALUES (6, 'Cust6', '6', 'c@example.invalid', '3x4', 'matte', 'standard', 'paid', 'paid', '300', '300', ?, ?)",
        (now, now)
    )
    delivery.enqueue(
        conn, order_id=6, kind="file", local_path=str(fake_orig),
        remote_path="PhotoDoc_Orders/6_abcd.jpg", sha256=hashlib.sha256(b"processed-bytes").hexdigest(),
        size=len(b"processed-bytes"), artifact_role="unknown"
    )
    conn.commit()
    conn.close()

    try:
        path = operator_workflow.source_for_download(db, 6)
        assert path != fake_orig, "Unknown role file was returned as operator source download!"
    except Exception as exc:
        assert isinstance(exc, HTTPException)
        assert exc.status_code in (404, 409, 410)


def test_security_acceptance_j_mismatched_result_path_remains_quarantined(env):
    """
    HIGH-01 / HIGH-02:
    When order_results points to a missing file path, an unknown job pointing to another path
    must be quarantined and must not be claimed by delivery worker.
    """
    db, uploads = env["db"], env["uploads"]
    orig_file, orig_sha = _create_synthetic_source(uploads, "7_orig.jpg")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db)
    operator_workflow.initialize(conn)
    conn.execute(
        "INSERT INTO orders (id, name, phone, email, format, paper, crop, status, payment_status, order_amount, payment_amount, paid_at, created_at) "
        "VALUES (7, 'Cust7', '7', 'c@example.invalid', '3x4', 'matte', 'standard', 'paid', 'paid', '300', '300', ?, ?)",
        (now, now)
    )
    jid = delivery.enqueue(
        conn, order_id=7, kind="file", local_path=str(orig_file),
        remote_path="PhotoDoc_AI/7_result.bin", sha256=orig_sha, size=orig_file.stat().st_size,
        artifact_role="unknown"
    )
    missing = uploads / "7_missing_result.jpg"
    conn.execute(
        "INSERT INTO order_results (order_id, path, sha256, size, version, updated_at) VALUES (7, ?, ?, ?, 1, ?)",
        (str(missing), orig_sha, orig_file.stat().st_size, now)
    )
    conn.commit()
    conn.close()

    reconcile(db, apply=True, uploads_dir=uploads)

    conn = sqlite3.connect(db)
    role = conn.execute("SELECT artifact_role FROM delivery_jobs WHERE id=?", (jid,)).fetchone()[0]
    conn.close()

    assert role == "quarantine"
    assert delivery.claim_due(db) is None
    assert orig_file.exists()


def test_security_acceptance_k_invalid_role_never_claimed_or_cleaned(env):
    """
    HIGH-01 / HIGH-02:
    An invalid role string ('invalid-role') is never claimed by worker and never cleaned up.
    """
    db, uploads = env["db"], env["uploads"]
    orig_file, orig_sha = _create_synthetic_source(uploads, "8_invalid.jpg")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO orders (id, name, phone, status, payment_status, created_at) "
        "VALUES (8, 'Cust8', '8', 'paid', 'paid', ?)", (now,)
    )
    jid = delivery.enqueue(
        conn, order_id=8, kind="file", local_path=str(orig_file),
        remote_path="PhotoDoc_Orders/8_result.bin", sha256=orig_sha, size=orig_file.stat().st_size,
        artifact_role="invalid-role"
    )
    conn.execute("UPDATE delivery_jobs SET state='delivered', delivered_at=? WHERE id=?", (now, jid))
    conn.commit()
    conn.close()

    assert delivery.claim_due(db) is None
    cleaned = delivery.perform_job_cleanup(db, jid, uploads_dir=uploads)
    assert cleaned is False
    assert orig_file.exists()



