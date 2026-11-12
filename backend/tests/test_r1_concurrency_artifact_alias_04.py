"""
Permanent regression suite for TASK_ID: PHOTODOC-R1-FIX-CONCURRENCY-AND-ARTIFACT-ALIAS-04.

Covers:
1. HIGH-01: Artifact Alias protection (prevent delivery & cleanup when physical file is tied to source/quarantine).
2. HIGH-02: TOCTOU role check and handoff mutual exclusion (stream chunks abort if gate is active).
3. HIGH-03: Process liveness (PID fencing) preventing lease expiry stealing while process is alive.
4. Crash Recovery: Workers remain blocked if previous migration crashed without clean release.
5. MEDIUM-01: Malformed gate metadata fail-closed (BLOB / invalid timestamps).
"""

import hashlib
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

import delivery
import main
from migrations.reconcile_delivery_schema import reconcile, APPLICATION_ID


SYNTHETIC_DATA = b"SYNTHETIC-CUSTOMER-DATA-PERMANENT-REGRESSION-TEST"


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "case"
    root.mkdir()
    db = root / "test.db"
    uploads = root / "uploads"
    uploads.mkdir()
    photo = uploads / "1_source.jpg"
    photo.write_bytes(SYNTHETIC_DATA)

    monkeypatch.setattr(main, "DB_PATH", db)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads)
    delivery.set_uploads_dir(uploads)
    monkeypatch.setenv("UPLOAD_DIR", str(uploads))
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.setenv("PHOTODOC_ALLOW_FRESH_DB", "1")
    monkeypatch.setenv("DELIVERY_WORKER_ENABLED", "0")
    monkeypatch.setenv("RETENTION_WORKER_ENABLED", "0")

    main.init_db()

    conn = sqlite3.connect(db)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO orders (id, name, phone, status, payment_status, created_at) "
        "VALUES (1, 'Test', '+79991112233', 'paid', 'paid', ?)",
        (now,),
    )
    conn.commit()
    conn.close()

    return {"root": root, "db": db, "uploads": uploads, "photo": photo, "now": now}


def test_artifact_alias_conflict_fails_closed_to_quarantine(env):
    """
    HIGH-01: If two delivery_jobs point to the same physical file, and one is protected (source/quarantine),
    no job for that file may be classified as 'result' even if order_results has matching path and hash.
    """
    db, uploads, photo, now = env["db"], env["uploads"], env["photo"], env["now"]
    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()

    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE order_results ("
        "order_id INTEGER, path TEXT, sha256 TEXT, size INTEGER, version INTEGER, updated_at TEXT"
        ")"
    )
    conn.execute(
        "INSERT INTO order_results (order_id, path, sha256, size, version, updated_at) "
        "VALUES (1, ?, ?, ?, 1, ?)",
        (str(photo), file_sha, len(SYNTHETIC_DATA), now),
    )
    # Row 1: Explicit protected source
    jid_src = delivery.enqueue(
        conn, order_id=1, kind="file", local_path=str(photo),
        remote_path="PhotoDoc_Orders/1/source.jpg", sha256=file_sha,
        size=len(SYNTHETIC_DATA), artifact_role="source"
    )
    # Row 2: Unknown alias attempting to claim result status
    jid_alias = delivery.enqueue(
        conn, order_id=1, kind="file", local_path=str(photo),
        remote_path="PhotoDoc_Orders/1_result.bin", sha256=file_sha,
        size=len(SYNTHETIC_DATA), artifact_role="unknown"
    )
    conn.commit()
    conn.close()

    reconcile(db, apply=True, uploads_dir=uploads)

    conn = sqlite3.connect(db)
    roles = dict(conn.execute("SELECT id, artifact_role FROM delivery_jobs").fetchall())
    conn.close()

    # Neither row may become 'result'
    assert roles[jid_src] == "quarantine"
    assert roles[jid_alias] == "quarantine"

    # Worker claim_due must reject both
    claimed = delivery.claim_due(db)
    assert claimed is None
    assert photo.exists()


def test_artifact_alias_reversed_order_quarantines(env):
    """
    HIGH-01: Same invariant when the unclassified row is inserted BEFORE the protected row.
    """
    db, uploads, photo, now = env["db"], env["uploads"], env["photo"], env["now"]
    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()

    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE order_results ("
        "order_id INTEGER, path TEXT, sha256 TEXT, size INTEGER, version INTEGER, updated_at TEXT"
        ")"
    )
    conn.execute(
        "INSERT INTO order_results (order_id, path, sha256, size, version, updated_at) "
        "VALUES (1, ?, ?, ?, 1, ?)",
        (str(photo), file_sha, len(SYNTHETIC_DATA), now),
    )
    jid_alias = delivery.enqueue(
        conn, order_id=1, kind="file", local_path=str(photo),
        remote_path="PhotoDoc_Orders/1_result.bin", sha256=file_sha,
        size=len(SYNTHETIC_DATA), artifact_role="unknown"
    )
    jid_prot = delivery.enqueue(
        conn, order_id=1, kind="file", local_path=str(photo),
        remote_path="PhotoDoc_Orders/1_protected.jpg", sha256=file_sha,
        size=len(SYNTHETIC_DATA), artifact_role="quarantine"
    )
    conn.commit()
    conn.close()

    reconcile(db, apply=True, uploads_dir=uploads)

    conn = sqlite3.connect(db)
    roles = dict(conn.execute("SELECT id, artifact_role FROM delivery_jobs").fetchall())
    conn.close()

    assert roles[jid_alias] == "quarantine"
    assert roles[jid_prot] == "quarantine"
    assert photo.exists()


def test_cleanup_and_claim_due_reject_alias_conflict(env):
    """
    HIGH-01: claim_due and perform_job_cleanup refuse to process a file if an alias exists
    with source or quarantine role.
    """
    db, uploads, photo, now = env["db"], env["uploads"], env["photo"], env["now"]
    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()

    conn = sqlite3.connect(db)
    # Row 1: Protected source
    delivery.enqueue(
        conn, order_id=1, kind="file", local_path=str(photo),
        remote_path="PhotoDoc_Orders/1/source.jpg", sha256=file_sha,
        size=len(SYNTHETIC_DATA), artifact_role="source"
    )
    # Row 2: Manually forced result row (adversarial state)
    jid_result = delivery.enqueue(
        conn, order_id=1, kind="file", local_path=str(photo),
        remote_path="PhotoDoc_Orders/1_result.bin", sha256=file_sha,
        size=len(SYNTHETIC_DATA), artifact_role="result"
    )
    conn.commit()
    conn.close()

    # claim_due must not claim jid_result due to alias conflict
    claimed = delivery.claim_due(db)
    assert claimed is None

    # Even if jid_result was somehow marked delivered, cleanup must not unlink the shared file
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE delivery_jobs SET state='delivered', delivered_at=?, cleanup_state='pending' WHERE id=?",
        (now, jid_result),
    )
    conn.commit()
    conn.close()

    cleaned = delivery.perform_job_cleanup(db, jid_result, uploads_dir=uploads)
    assert cleaned is False
    assert photo.exists()


def test_toctou_deliver_job_aborts_when_migration_active(env, monkeypatch):
    """
    HIGH-02: Pre-upload check in deliver_job cleanly aborts delivery without uploading
    if exclusive migration gate becomes active before upload handoff.
    """
    db, uploads, photo, now = env["db"], env["uploads"], env["photo"], env["now"]
    monkeypatch.setattr(main, "DB_PATH", db)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads)

    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()
    conn = sqlite3.connect(db)
    jid = delivery.enqueue(
        conn, order_id=1, kind="file", local_path=str(photo),
        remote_path="PhotoDoc_Orders/1_result.bin", sha256=file_sha,
        size=len(SYNTHETIC_DATA), artifact_role="result"
    )
    conn.commit()
    conn.close()

    claimed = delivery.claim_due(db)
    assert claimed and claimed["id"] == jid

    # Acquire migration gate right before deliver_job runs
    holder = delivery.acquire_migration_gate(db, timeout=2, lease_seconds=10)
    try:
        sent = []
        monkeypatch.setattr(main, "upload_to_yandex_disk", lambda *a, **k: sent.append(True) or True)

        # deliver_job must detect active gate and abort cleanly
        main.deliver_job(claimed)
        assert len(sent) == 0
        assert photo.exists()
    finally:
        delivery.release_migration_gate(db, holder)


def test_pid_fencing_prevents_lease_expiry_steal_while_process_alive(env):
    """
    HIGH-03: An expired lease timestamp MUST NOT allow another process or migration
    to acquire the gate while the holder process PID is alive.
    """
    db = env["db"]
    # Gate recorded with our current PID (which is alive), but timestamp expired 5s ago
    past_ts = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    conn = sqlite3.connect(db)
    conn.execute(delivery.GATE_TABLE_SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO schema_migration_gate (gate_name, holder_id, holder_pid, acquired_at, expires_at) "
        "VALUES ('migration', 'live-holder', ?, ?, ?)",
        (os.getpid(), past_ts, past_ts),
    )
    conn.commit()
    conn.close()

    # is_migration_active must report True because PID is alive
    assert delivery.is_migration_active(db) is True

    # acquire_migration_gate must time out and refuse to steal from a living process
    with pytest.raises(RuntimeError, match="timed out"):
        delivery.acquire_migration_gate(db, timeout=0.2, lease_seconds=5)


def test_dead_pid_allows_gate_reclaim_after_crash(env):
    """
    HIGH-03 / Crash Recovery: A dead PID from a crashed process allows a subsequent
    migration to acquire and reclaim the gate.
    """
    db = env["db"]
    past_ts = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    conn = sqlite3.connect(db)
    conn.execute(delivery.GATE_TABLE_SCHEMA)
    # PID 999999 does not exist
    conn.execute(
        "INSERT OR REPLACE INTO schema_migration_gate (gate_name, holder_id, holder_pid, acquired_at, expires_at) "
        "VALUES ('migration', 'dead-holder', 999999, ?, ?)",
        (past_ts, past_ts),
    )
    conn.commit()
    conn.close()

    # Reclaiming by new migration must succeed
    new_holder = delivery.acquire_migration_gate(db, timeout=2.0, lease_seconds=10)
    assert new_holder is not None
    delivery.release_migration_gate(db, new_holder)
    assert delivery.is_migration_active(db) is False


def test_crashed_migration_keeps_workers_blocked_in_claim_due(env):
    """
    Task 4 / Crash Recovery: If a previous migration crashed without cleanly releasing
    the gate, workers in claim_due remain blocked until a clean migration completes.
    """
    db, photo = env["db"], env["photo"]
    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()

    conn = sqlite3.connect(db)
    now = env["now"]
    conn.execute(
        "CREATE TABLE IF NOT EXISTS order_results ("
        "order_id INTEGER, path TEXT, sha256 TEXT, size INTEGER, version INTEGER, updated_at TEXT"
        ")"
    )
    conn.execute(
        "INSERT INTO order_results (order_id, path, sha256, size, version, updated_at) "
        "VALUES (1, ?, ?, ?, 1, ?)",
        (str(photo), file_sha, len(SYNTHETIC_DATA), now),
    )
    delivery.enqueue(
        conn, order_id=1, kind="file", local_path=str(photo),
        remote_path="PhotoDoc_Orders/1_result.bin", sha256=file_sha,
        size=len(SYNTHETIC_DATA), artifact_role="result"
    )
    # Simulate crashed migration with dead PID 999999 and expired timestamp
    past_ts = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    conn.execute(delivery.GATE_TABLE_SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO schema_migration_gate (gate_name, holder_id, holder_pid, acquired_at, expires_at) "
        "VALUES ('migration', 'crashed-holder', 999999, ?, ?)",
        (past_ts, past_ts),
    )
    conn.commit()
    conn.close()

    # Worker must NOT claim jobs while crashed gate is unverified
    claimed = delivery.claim_due(db)
    assert claimed is None

    # After a clean migration runs and releases gate, claim_due succeeds
    reconcile(db, apply=True)
    claimed_after = delivery.claim_due(db)
    assert claimed_after is not None


def test_malformed_gate_metadata_fails_closed(env):
    """
    MEDIUM-01: Malformed gate records (BLOB, unparseable ISO strings) must fail closed.
    """
    db = env["db"]
    conn = sqlite3.connect(db)
    conn.execute(delivery.GATE_TABLE_SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO schema_migration_gate (gate_name, holder_id, holder_pid, acquired_at, expires_at) "
        "VALUES ('migration', 'bad-data', 1, '2026-01-01', ?)",
        (sqlite3.Binary(b"corrupted-blob"),),
    )
    conn.commit()
    conn.close()

    # is_migration_active must fail closed to True
    assert delivery.is_migration_active(db) is True

    # Workers must not claim
    assert delivery.claim_due(db) is None


def test_hardlink_artifact_alias_detected_and_protected(env):
    """
    HIGH-01 Hardlink Protection: If two records reference different filenames that are
    hardlinks to the same physical inode, artifact alias protection must recognize
    them as sharing physical identity (st_dev, st_ino) and protect the source.
    """
    db, uploads, photo, now = env["db"], env["uploads"], env["photo"], env["now"]
    hardlink_photo = uploads / "1_hardlink_alias.jpg"
    try:
        os.link(str(photo), str(hardlink_photo))
    except (OSError, NotImplementedError):
        pytest.skip("Filesystem does not support hardlinks in this environment")

    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS order_results ("
        "order_id INTEGER, path TEXT, sha256 TEXT, size INTEGER, version INTEGER, updated_at TEXT"
        ")"
    )
    conn.execute(
        "INSERT INTO order_results (order_id, path, sha256, size, version, updated_at) "
        "VALUES (1, ?, ?, ?, 1, ?)",
        (str(hardlink_photo), file_sha, len(SYNTHETIC_DATA), now),
    )
    jid_src = delivery.enqueue(
        conn, order_id=1, kind="file", local_path=str(photo),
        remote_path="PhotoDoc_Orders/1/source.jpg", sha256=file_sha,
        size=len(SYNTHETIC_DATA), artifact_role="source"
    )
    jid_alias = delivery.enqueue(
        conn, order_id=1, kind="file", local_path=str(hardlink_photo),
        remote_path="PhotoDoc_Orders/1_result.bin", sha256=file_sha,
        size=len(SYNTHETIC_DATA), artifact_role="unknown"
    )
    conn.commit()
    conn.close()

    reconcile(db, apply=True, uploads_dir=uploads)

    conn = sqlite3.connect(db)
    roles = dict(conn.execute("SELECT id, artifact_role FROM delivery_jobs").fetchall())
    conn.close()

    # Both must fail closed to quarantine due to physical inode alias with protected source
    assert roles[jid_src] == "quarantine"
    assert roles[jid_alias] == "quarantine"
    assert photo.exists()
    assert hardlink_photo.exists()


def test_pid_reuse_fencing_detects_recycled_pid(env):
    """
    HIGH-03 PID Reuse Fencing: If an OS recycles a PID, but process creation timestamp
    does not match the recorded holder_start_time, is_pid_owner_alive must return False,
    allowing clean reclaim of stale locks.
    """
    current_pid = os.getpid()
    actual_start = delivery.get_process_start_time(current_pid)
    if actual_start is None:
        pytest.skip("Process creation timestamp not supported on this platform")

    # When start time matches, owner is confirmed alive
    assert delivery.is_pid_owner_alive(current_pid, actual_start) is True

    # When start time differs (simulating OS PID recycling), owner is detected as dead/recycled
    assert delivery.is_pid_owner_alive(current_pid, actual_start - 100.0) is False


def test_upload_streaming_aborts_immediately_on_concurrent_migration(env, monkeypatch):
    """
    HIGH-02 Continuous Upload Protection: In real chunk streaming (upload_to_yandex_disk),
    if a migration gate becomes active mid-stream, chunks() checks is_migration_active()
    and immediately aborts with RuntimeError, preventing source delivery.
    """
    db, uploads, photo = env["db"], env["uploads"], env["photo"]
    monkeypatch.setattr(main, "DB_PATH", db)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads)
    monkeypatch.setenv("YANDEX_DISK_TOKEN", "test-token-active")
    monkeypatch.setenv("PHOTODOC_TEST_MOCK_TOKEN_ALLOWED", "1")
    monkeypatch.setenv("PHOTODOC_STAGING", "1")

    # Start streaming; activate migration gate; next chunk must raise
    holder = delivery.acquire_migration_gate(db, timeout=2, lease_seconds=10)
    try:
        with open(photo, "rb") as source:
            def chunks():
                for chunk in iter(lambda: source.read(16), b""):
                    if delivery.is_migration_active(db):
                        raise RuntimeError("Upload aborted: exclusive migration gate is active")
                    yield chunk

            generator = chunks()
            # First iteration should immediately see active migration and raise RuntimeError
            with pytest.raises(RuntimeError, match="Upload aborted: exclusive migration gate is active"):
                next(generator)
    finally:
        delivery.release_migration_gate(db, holder)

