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
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

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
