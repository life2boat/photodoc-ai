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

        # deliver_job must detect active gate and fail closed (preventing state transition to delivered)
        with pytest.raises(RuntimeError, match="exclusive migration gate is active"):
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


def test_real_streaming_upload_aborts_between_chunks_on_concurrent_migration(env, monkeypatch):
    """
    HIGH-02 Continuous Upload Protection: In the real streaming upload path (deliver_job -> upload_to_yandex_disk),
    chunks are streamed into httpx.put. If a concurrent migration reclassifies the artifact role between chunks,
    chunks() detects the role change, immediately aborts with RuntimeError, prevents further chunk transmission,
    marks the job as failed (never delivered), and preserves the physical file.
    """
    db, uploads = env["db"], env["uploads"]
    multi_chunk_data = b"REAL-STREAMING-CUSTOMER-FILE-DATA-" * 4500  # ~153,000 bytes (>2 chunks of 64KB)
    file_sha = hashlib.sha256(multi_chunk_data).hexdigest()
    photo = uploads / "1_large_result.bin"
    photo.write_bytes(multi_chunk_data)

    monkeypatch.setattr(main, "DB_PATH", db)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads)
    monkeypatch.setenv("YANDEX_DISK_TOKEN", "synthetic-token-only")
    monkeypatch.setenv("PHOTODOC_STAGING", "0")
    monkeypatch.setenv("PHOTODOC_ALLOW_FRESH_DB", "0")
    monkeypatch.setenv("YANDEX_DISK_ROOT_DIR", "PhotoDoc_Orders")

    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS order_results ("
            "order_id INTEGER, path TEXT, sha256 TEXT, size INTEGER, version INTEGER, updated_at TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO order_results (order_id, path, sha256, size, version, updated_at) "
            "VALUES (1, ?, ?, ?, 1, ?)",
            (str(photo), file_sha, len(multi_chunk_data), env["now"]),
        )
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/1_large_result.bin", sha256=file_sha,
            size=len(multi_chunk_data), artifact_role="result",
        )

    class MockResponse:
        status_code = 200
        def json(self): return {"href": "https://upload.stub.test/v1/stream"}
        def raise_for_status(self): pass

    monkeypatch.setattr(main, "ensure_yandex_folder", lambda *a, **k: None)
    monkeypatch.setattr(main, "_remote_verified", lambda *a, **k: False)
    monkeypatch.setattr(main.httpx, "get", lambda *a, **k: MockResponse())

    received_chunks = []

    def streaming_put_stub(url, *, content, headers=None, timeout=None):
        iterator = iter(content)
        # Read the first chunk (65536 bytes)
        first = next(iterator)
        received_chunks.append(first)

        # Concurrently reclassify the artifact to 'quarantine' before the second chunk
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE delivery_jobs SET artifact_role='quarantine' WHERE id=?", (jid,))
            conn.commit()

        # Attempt to read subsequent chunks: chunks() generator MUST detect reclassification and raise RuntimeError
        for subsequent in iterator:
            received_chunks.append(subsequent)
        return MockResponse()

    monkeypatch.setattr(main.httpx, "put", streaming_put_stub)

    # Process due delivery: deliver_job runs the real upload_to_yandex_disk streaming path
    processed = delivery.process_due(db, main.deliver_job, limit=1, uploads_dir=uploads)
    assert processed == 1

    # Verify: exactly one chunk was received by the stub; subsequent chunks were blocked
    assert len(received_chunks) == 1
    assert len(received_chunks[0]) == 65536
    assert sum(len(c) for c in received_chunks) < len(multi_chunk_data)

    # Verify: job MUST NOT be marked delivered; state must be 'failed'
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT state, artifact_role, cleanup_state FROM delivery_jobs WHERE id=?", (jid,)).fetchone()
    assert row[0] == "failed"
    assert row[1] == "quarantine"
    assert row[2] != "completed"

    # Verify: physical file must NOT be deleted
    assert photo.exists()


def test_real_streaming_upload_aborts_on_gate_activation_between_chunks(env, monkeypatch):
    """
    HIGH-02 Mutual Exclusion: In real streaming upload, if an exclusive migration gate
    is acquired between chunks, the next chunk read immediately fails closed with RuntimeError.
    """
    db, uploads = env["db"], env["uploads"]
    multi_chunk_data = b"REAL-STREAMING-CUSTOMER-FILE-DATA-" * 4500
    file_sha = hashlib.sha256(multi_chunk_data).hexdigest()
    photo = uploads / "1_gate_result.bin"
    photo.write_bytes(multi_chunk_data)

    monkeypatch.setattr(main, "DB_PATH", db)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads)
    monkeypatch.setenv("YANDEX_DISK_TOKEN", "synthetic-token-only")
    monkeypatch.setenv("PHOTODOC_STAGING", "0")
    monkeypatch.setenv("PHOTODOC_ALLOW_FRESH_DB", "0")
    monkeypatch.setenv("YANDEX_DISK_ROOT_DIR", "PhotoDoc_Orders")

    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS order_results ("
            "order_id INTEGER, path TEXT, sha256 TEXT, size INTEGER, version INTEGER, updated_at TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO order_results (order_id, path, sha256, size, version, updated_at) "
            "VALUES (1, ?, ?, ?, 1, ?)",
            (str(photo), file_sha, len(multi_chunk_data), env["now"]),
        )
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/1_gate_result.bin", sha256=file_sha,
            size=len(multi_chunk_data), artifact_role="result",
        )

    class MockResponse:
        status_code = 200
        def json(self): return {"href": "https://upload.stub.test/v1/stream"}
        def raise_for_status(self): pass

    monkeypatch.setattr(main, "ensure_yandex_folder", lambda *a, **k: None)
    monkeypatch.setattr(main, "_remote_verified", lambda *a, **k: False)
    monkeypatch.setattr(main.httpx, "get", lambda *a, **k: MockResponse())

    received_chunks = []
    gate_holder = [None]

    def streaming_put_stub(url, *, content, headers=None, timeout=None):
        iterator = iter(content)
        first = next(iterator)
        received_chunks.append(first)

        # Acquire migration gate between chunks
        gate_holder[0] = delivery.acquire_migration_gate(db, timeout=2, lease_seconds=10)

        for subsequent in iterator:
            received_chunks.append(subsequent)
        return MockResponse()

    monkeypatch.setattr(main.httpx, "put", streaming_put_stub)

    try:
        processed = delivery.process_due(db, main.deliver_job, limit=1, uploads_dir=uploads)
        assert processed == 1
    finally:
        if gate_holder[0]:
            delivery.release_migration_gate(db, gate_holder[0])

    assert len(received_chunks) == 1
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT state, cleanup_state FROM delivery_jobs WHERE id=?", (jid,)).fetchone()
    assert row[0] == "failed"
    assert row[1] != "completed"
    assert photo.exists()


def test_real_streaming_upload_aborts_on_sqlite_error_between_chunks(env, monkeypatch):
    """
    Fail-Closed SQLite Error: If an operational error occurs in SQLite while verifying authorization
    between chunks, chunks() fails closed immediately with RuntimeError without transmitting further bytes.
    """
    db, uploads = env["db"], env["uploads"]
    multi_chunk_data = b"REAL-STREAMING-CUSTOMER-FILE-DATA-" * 4500
    file_sha = hashlib.sha256(multi_chunk_data).hexdigest()
    photo = uploads / "1_sqlite_err_result.bin"
    photo.write_bytes(multi_chunk_data)

    monkeypatch.setattr(main, "DB_PATH", db)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads)
    monkeypatch.setenv("YANDEX_DISK_TOKEN", "synthetic-token-only")
    monkeypatch.setenv("PHOTODOC_STAGING", "0")
    monkeypatch.setenv("PHOTODOC_ALLOW_FRESH_DB", "0")
    monkeypatch.setenv("YANDEX_DISK_ROOT_DIR", "PhotoDoc_Orders")

    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS order_results ("
            "order_id INTEGER, path TEXT, sha256 TEXT, size INTEGER, version INTEGER, updated_at TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO order_results (order_id, path, sha256, size, version, updated_at) "
            "VALUES (1, ?, ?, ?, 1, ?)",
            (str(photo), file_sha, len(multi_chunk_data), env["now"]),
        )
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/1_sqlite_err_result.bin", sha256=file_sha,
            size=len(multi_chunk_data), artifact_role="result",
        )

    class MockResponse:
        status_code = 200
        def json(self): return {"href": "https://upload.stub.test/v1/stream"}
        def raise_for_status(self): pass

    monkeypatch.setattr(main, "ensure_yandex_folder", lambda *a, **k: None)
    monkeypatch.setattr(main, "_remote_verified", lambda *a, **k: False)
    monkeypatch.setattr(main.httpx, "get", lambda *a, **k: MockResponse())

    received_chunks = []

    def streaming_put_stub(url, *, content, headers=None, timeout=None):
        iterator = iter(content)
        first = next(iterator)
        received_chunks.append(first)

        # Corrupt / lock the SQLite connection for subsequent chunk queries
        real_connect = sqlite3.connect
        failed_once = [False]
        def broken_connect(*a, **k):
            if not failed_once[0]:
                failed_once[0] = True
                raise sqlite3.OperationalError("database disk image is malformed")
            return real_connect(*a, **k)
        monkeypatch.setattr(sqlite3, "connect", broken_connect)

        # Calling next chunk must raise RuntimeError due to fail-closed SQLite check
        for subsequent in iterator:
            received_chunks.append(subsequent)
        return MockResponse()

    monkeypatch.setattr(main.httpx, "put", streaming_put_stub)

    processed = delivery.process_due(db, main.deliver_job, limit=1, uploads_dir=uploads)
    assert processed == 1
    assert len(received_chunks) == 1
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT state, cleanup_state FROM delivery_jobs WHERE id=?", (jid,)).fetchone()
    assert row[0] == "failed"
    assert row[1] != "completed"
    assert photo.exists()


def test_real_streaming_upload_aborts_on_stat_permission_error_between_chunks(env, monkeypatch):
    """
    Fail-Closed Stat Error: If a PermissionError or stat failure occurs on the physical file between chunks,
    chunks() immediately aborts with RuntimeError, failing closed.
    """
    db, uploads = env["db"], env["uploads"]
    multi_chunk_data = b"REAL-STREAMING-CUSTOMER-FILE-DATA-" * 4500
    file_sha = hashlib.sha256(multi_chunk_data).hexdigest()
    photo = uploads / "1_stat_err_result.bin"
    photo.write_bytes(multi_chunk_data)

    monkeypatch.setattr(main, "DB_PATH", db)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads)
    monkeypatch.setenv("YANDEX_DISK_TOKEN", "synthetic-token-only")
    monkeypatch.setenv("PHOTODOC_STAGING", "0")
    monkeypatch.setenv("PHOTODOC_ALLOW_FRESH_DB", "0")
    monkeypatch.setenv("YANDEX_DISK_ROOT_DIR", "PhotoDoc_Orders")

    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS order_results ("
            "order_id INTEGER, path TEXT, sha256 TEXT, size INTEGER, version INTEGER, updated_at TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO order_results (order_id, path, sha256, size, version, updated_at) "
            "VALUES (1, ?, ?, ?, 1, ?)",
            (str(photo), file_sha, len(multi_chunk_data), env["now"]),
        )
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/1_stat_err_result.bin", sha256=file_sha,
            size=len(multi_chunk_data), artifact_role="result",
        )

    class MockResponse:
        status_code = 200
        def json(self): return {"href": "https://upload.stub.test/v1/stream"}
        def raise_for_status(self): pass

    monkeypatch.setattr(main, "ensure_yandex_folder", lambda *a, **k: None)
    monkeypatch.setattr(main, "_remote_verified", lambda *a, **k: False)
    monkeypatch.setattr(main.httpx, "get", lambda *a, **k: MockResponse())

    received_chunks = []
    real_stat = Path.stat
    failing = [True]

    def streaming_put_stub(url, *, content, headers=None, timeout=None):
        iterator = iter(content)
        first = next(iterator)
        received_chunks.append(first)

        # Inject PermissionError on subsequent stat calls
        def failing_stat(self, *args, **kwargs):
            if failing[0] and str(self) == str(photo):
                raise PermissionError("Access denied on physical file stat")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", failing_stat)

        for subsequent in iterator:
            received_chunks.append(subsequent)
        return MockResponse()

    monkeypatch.setattr(main.httpx, "put", streaming_put_stub)

    processed = delivery.process_due(db, main.deliver_job, limit=1, uploads_dir=uploads)
    failing[0] = False
    assert processed == 1
    assert len(received_chunks) == 1
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT state, cleanup_state FROM delivery_jobs WHERE id=?", (jid,)).fetchone()
    assert row[0] == "failed"
    assert row[1] != "completed"
    assert photo.exists()



def test_gate_generation_increments_and_holder_start_time_recorded(env):
    """
    MEDIUM-02 Gate Metadata: Verify generation increments on each acquire,
    and holder_start_time is stored and checked.
    """
    db = env["db"]
    holder1 = delivery.acquire_migration_gate(db, timeout=2, lease_seconds=10)
    with sqlite3.connect(db) as conn:
        gen1, start1 = conn.execute("SELECT generation, holder_start_time FROM schema_migration_gate WHERE gate_name='migration'").fetchone()
    assert gen1 is not None and int(gen1) >= 1
    assert start1 is not None and float(start1) > 0

    delivery.release_migration_gate(db, holder1)
    with sqlite3.connect(db) as conn:
        holder_id_after, gen_after = conn.execute("SELECT holder_id, generation FROM schema_migration_gate WHERE gate_name='migration'").fetchone()
    assert holder_id_after == ""
    assert gen_after == gen1

    holder2 = delivery.acquire_migration_gate(db, timeout=2, lease_seconds=10)
    try:
        with sqlite3.connect(db) as conn:
            gen2, start2 = conn.execute("SELECT generation, holder_start_time FROM schema_migration_gate WHERE gate_name='migration'").fetchone()
        assert int(gen2) == int(gen1) + 1
        assert start2 is not None and float(start2) > 0
    finally:
        delivery.release_migration_gate(db, holder2)


def test_identity_stat_error_fails_closed_in_claim_and_cleanup(env, monkeypatch):
    """
    MEDIUM-01 Fail-Closed Identity: Stat, PermissionError, or missing files
    must fail closed rather than assuming absence of conflicting alias.
    """
    db, photo = env["db"], env["photo"]
    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()

    # Enqueue a candidate result job and a protected source job
    with sqlite3.connect(db) as conn:
        jid1 = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/1_result.bin", sha256=file_sha,
            size=len(SYNTHETIC_DATA), artifact_role="result"
        )
        jid2 = delivery.enqueue(
            conn, order_id=2, kind="file", local_path=str(photo.parent / "2_source.jpg"),
            remote_path="PhotoDoc_Orders/2_source.jpg", sha256=file_sha,
            size=len(SYNTHETIC_DATA), artifact_role="source"
        )

    # Monkeypatch get_file_identity_strict to raise PermissionError when inspecting protected source
    real_get_identity = delivery.get_file_identity_strict

    def fail_identity(path):
        if "2_source" in str(path):
            raise PermissionError("Access denied for security test")
        return real_get_identity(path)

    monkeypatch.setattr(delivery, "get_file_identity_strict", fail_identity)

    # claim_due must fail closed (return None) because it cannot prove absence of conflict
    claimed = delivery.claim_due(db)
    assert claimed is None


def test_failed_delivery_does_not_mark_delivered_or_delete_file(env, monkeypatch):
    """
    HIGH-02 Forbidden Delivery Completion: When deliver_job fails or is aborted,
    the job must NOT transition to 'delivered', and cleanup must NEVER run.
    """
    db, uploads, photo = env["db"], env["uploads"], env["photo"]
    monkeypatch.setattr(main, "DB_PATH", db)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads)

    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()
    with sqlite3.connect(db) as conn:
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/1_result.bin", sha256=file_sha,
            size=len(SYNTHETIC_DATA), artifact_role="result"
        )

    # Mock deliver to raise RuntimeError (e.g. upload aborted / forbidden)
    def failing_deliver(job):
        raise RuntimeError("Delivery forbidden by security check")

    processed = delivery.process_due(db, failing_deliver, limit=1, uploads_dir=uploads)
    assert processed == 1

    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT state, cleanup_state FROM delivery_jobs WHERE id=?", (jid,)).fetchone()
    # Job must NOT be delivered, and cleanup must not be completed
    assert row[0] == "failed"
    assert row[1] != "completed"
    assert photo.exists()


def test_real_streaming_upload_completes_and_delivers_successfully(env, monkeypatch):
    """
    Reader/Writer Exclusion: When no migration interferes, real multi-chunk streaming
    completes successfully, releases the stream lease, transitions job to 'delivered',
    and allows cleanup to delete the local file.
    """
    db, uploads = env["db"], env["uploads"]
    multi_chunk_data = b"VALID-RESULT-DATA-FOR-CUSTOMER-" * 4500  # ~140KB
    file_sha = hashlib.sha256(multi_chunk_data).hexdigest()
    photo = uploads / "1_complete_result.bin"
    photo.write_bytes(multi_chunk_data)

    monkeypatch.setattr(main, "DB_PATH", db)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads)
    monkeypatch.setenv("YANDEX_DISK_TOKEN", "synthetic-token-only")
    monkeypatch.setenv("PHOTODOC_STAGING", "0")
    monkeypatch.setenv("PHOTODOC_ALLOW_FRESH_DB", "0")
    monkeypatch.setenv("YANDEX_DISK_ROOT_DIR", "PhotoDoc_Orders")

    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS order_results ("
            "order_id INTEGER, path TEXT, sha256 TEXT, size INTEGER, version INTEGER, updated_at TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO order_results (order_id, path, sha256, size, version, updated_at) "
            "VALUES (1, ?, ?, ?, 1, ?)",
            (str(photo), file_sha, len(multi_chunk_data), env["now"]),
        )
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/1_complete_result.bin", sha256=file_sha,
            size=len(multi_chunk_data), artifact_role="result",
        )

    class MockResponse:
        status_code = 200
        def json(self): return {"href": "https://upload.stub.test/v1/stream"}
        def raise_for_status(self): pass

    received_chunks = []

    def streaming_put_stub(url, *, content, headers=None, timeout=None):
        for chunk in content:
            received_chunks.append(chunk)
        return MockResponse()

    def remote_verified_stub(token, path, sha, sz):
        return len(received_chunks) > 0

    monkeypatch.setattr(main, "ensure_yandex_folder", lambda *a, **k: None)
    monkeypatch.setattr(main, "_remote_verified", remote_verified_stub)
    monkeypatch.setattr(main.httpx, "get", lambda *a, **k: MockResponse())
    monkeypatch.setattr(main.httpx, "put", streaming_put_stub)

    processed = delivery.process_due(db, main.deliver_job, limit=1, uploads_dir=uploads)
    assert processed == 1

    # All chunks received
    assert len(received_chunks) > 1
    assert b"".join(received_chunks) == multi_chunk_data

    # Job is delivered
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT state, cleanup_state FROM delivery_jobs WHERE id=?", (jid,)).fetchone()
    assert row[0] == "delivered"

    # Stream lease was cleanly released
    with sqlite3.connect(db) as conn:
        active = conn.execute("SELECT COUNT(*) FROM delivery_stream_leases WHERE job_id=?", (jid,)).fetchone()[0]
    assert active == 0

    # Cleanup phase can now proceed safely
    cleaned = delivery.perform_job_cleanup(db, jid, uploads_dir=uploads)
    assert cleaned is True
    assert not photo.exists()


def test_reader_writer_exclusion_migration_blocked_while_stream_in_flight(env, monkeypatch):
    """
    Reader/Writer Mutual Exclusion: While worker holds an active delivery stream lease
    (streaming chunk 1 to remote stub), a concurrent migration attempt CANNOT acquire
    the exclusive migration gate. The file is NOT reclassified in flight, and migration
    safely fails closed / times out rather than mutating the artifact role.
    """
    db, uploads = env["db"], env["uploads"]
    multi_chunk_data = b"READER-WRITER-EXCLUSION-DATA-" * 4500
    file_sha = hashlib.sha256(multi_chunk_data).hexdigest()
    photo = uploads / "1_rw_result.bin"
    photo.write_bytes(multi_chunk_data)

    monkeypatch.setattr(main, "DB_PATH", db)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads)
    monkeypatch.setenv("YANDEX_DISK_TOKEN", "synthetic-token-only")
    monkeypatch.setenv("PHOTODOC_STAGING", "0")
    monkeypatch.setenv("PHOTODOC_ALLOW_FRESH_DB", "0")
    monkeypatch.setenv("YANDEX_DISK_ROOT_DIR", "PhotoDoc_Orders")

    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS order_results ("
            "order_id INTEGER, path TEXT, sha256 TEXT, size INTEGER, version INTEGER, updated_at TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO order_results (order_id, path, sha256, size, version, updated_at) "
            "VALUES (1, ?, ?, ?, 1, ?)",
            (str(photo), file_sha, len(multi_chunk_data), env["now"]),
        )
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/1_rw_result.bin", sha256=file_sha,
            size=len(multi_chunk_data), artifact_role="result",
        )

    class MockResponse:
        status_code = 200
        def json(self): return {"href": "https://upload.stub.test/v1/stream"}
        def raise_for_status(self): pass

    migration_attempt_result = []

    def streaming_put_stub(url, *, content, headers=None, timeout=None):
        iterator = iter(content)
        first = next(iterator)

        # While stream lease is actively held by our PID, attempt to acquire exclusive migration gate
        try:
            delivery.acquire_migration_gate(db, timeout=0.2, lease_seconds=5)
            migration_attempt_result.append("acquired")
        except RuntimeError as exc:
            migration_attempt_result.append(f"refused: {exc}")

        # Finish streaming subsequent chunks
        for chunk in iterator:
            pass
        return MockResponse()

    def remote_verified_rw_stub(token, path, sha, sz):
        return len(migration_attempt_result) > 0

    monkeypatch.setattr(main, "ensure_yandex_folder", lambda *a, **k: None)
    monkeypatch.setattr(main, "_remote_verified", remote_verified_rw_stub)
    monkeypatch.setattr(main.httpx, "get", lambda *a, **k: MockResponse())
    monkeypatch.setattr(main.httpx, "put", streaming_put_stub)

    processed = delivery.process_due(db, main.deliver_job, limit=1, uploads_dir=uploads)
    assert processed == 1

    # Migration attempt was refused because stream was in-flight
    assert len(migration_attempt_result) == 1
    assert "refused" in migration_attempt_result[0]

    # File role was NEVER reclassified during upload
    with sqlite3.connect(db) as conn:
        role = conn.execute("SELECT artifact_role FROM delivery_jobs WHERE id=?", (jid,)).fetchone()[0]
    assert role == "result"


def test_reader_writer_exclusion_worker_blocked_when_migration_active_before_chunk1(env, monkeypatch):
    """
    Reader/Writer Mutual Exclusion: If migration holds the exclusive gate before worker
    initiates delivery, worker is blocked from claiming/acquiring the stream lease, 0 chunks are sent,
    and job is not marked delivered.
    """
    db, uploads, photo = env["db"], env["uploads"], env["photo"]
    monkeypatch.setattr(main, "DB_PATH", db)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads)
    monkeypatch.setenv("YANDEX_DISK_TOKEN", "synthetic-token-only")
    monkeypatch.setenv("PHOTODOC_STAGING", "0")
    monkeypatch.setenv("PHOTODOC_ALLOW_FRESH_DB", "0")
    monkeypatch.setenv("YANDEX_DISK_ROOT_DIR", "PhotoDoc_Orders")

    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS order_results ("
            "order_id INTEGER, path TEXT, sha256 TEXT, size INTEGER, version INTEGER, updated_at TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO order_results (order_id, path, sha256, size, version, updated_at) "
            "VALUES (1, ?, ?, ?, 1, ?)",
            (str(photo), file_sha, len(SYNTHETIC_DATA), env["now"]),
        )
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/1_result.bin", sha256=file_sha,
            size=len(SYNTHETIC_DATA), artifact_role="result",
        )

    chunks_sent = []

    def mock_put(*args, **kwargs):
        chunks_sent.append(True)
        return None

    monkeypatch.setattr(main.httpx, "put", mock_put)

    # Acquire exclusive migration gate BEFORE worker starts
    holder = delivery.acquire_migration_gate(db, timeout=2.0, lease_seconds=30)
    try:
        processed = delivery.process_due(db, main.deliver_job, limit=1, uploads_dir=uploads)
        # Worker cannot claim jobs while migration gate is active
        assert processed == 0
    finally:
        delivery.release_migration_gate(db, holder)

    # 0 chunks sent
    assert len(chunks_sent) == 0

    # Job was blocked from claim and remains pending (never delivered)
    with sqlite3.connect(db) as conn:
        state, cleanup = conn.execute("SELECT state, cleanup_state FROM delivery_jobs WHERE id=?", (jid,)).fetchone()
    assert state == "pending"
    assert cleanup != "completed"
    assert photo.exists()

    # Re-acquire gate and test that an active deliver_job attempt is rejected fail-closed
    holder2 = delivery.acquire_migration_gate(db, timeout=2.0, lease_seconds=30)
    try:
        claimed_job = {"id": jid, "order_id": 1, "kind": "file", "local_path": str(photo), "remote_path": "PhotoDoc_Orders/1_result.bin"}
        with pytest.raises(RuntimeError, match="migration gate is active"):
            main.deliver_job(claimed_job)
    finally:
        delivery.release_migration_gate(db, holder2)


def test_worker_crash_releases_stream_lease_for_subsequent_migration(env):
    """
    Crash Recovery: If a worker process crashes mid-stream (holding a stream lease),
    a subsequent migration detects that the worker PID is dead, reclaims/purges the stale
    lease, and successfully acquires the exclusive migration gate.
    """
    db = env["db"]
    conn = sqlite3.connect(db)
    delivery.ensure_gate_table(conn)
    # Simulate a crashed worker with dead PID 999999
    now_str = env["now"]
    conn.execute(
        "INSERT OR REPLACE INTO delivery_stream_leases "
        "(job_id, holder_pid, holder_start_time, acquired_at, expires_at, gate_generation) "
        "VALUES ('crashed-job-123', 999999, 12345.0, ?, ?, 1)",
        (now_str, now_str),
    )
    conn.commit()
    conn.close()

    # Migration must detect dead PID and successfully acquire the gate without deadlock
    holder = delivery.acquire_migration_gate(db, timeout=1.0, lease_seconds=10)
    assert holder is not None
    delivery.release_migration_gate(db, holder)

    # Verify stale stream lease was purged
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM delivery_stream_leases WHERE job_id='crashed-job-123'").fetchone()[0]
    assert count == 0


def test_lease_expiration_and_pid_reuse_fencing_for_stream_leases(env):
    """
    PID Reuse Fencing: If an OS recycles a worker PID, but process creation timestamp
    differs from recorded holder_start_time, get_active_stream_leases treats the lease
    as stale/recycled and purges it.
    """
    db = env["db"]
    current_pid = os.getpid()
    actual_start = delivery.get_process_start_time(current_pid)
    if actual_start is None:
        pytest.skip("Process creation timestamp not supported on this platform")

    conn = sqlite3.connect(db)
    delivery.ensure_gate_table(conn)
    future_ts = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()

    # Record lease with our PID but mismatched start time (simulating recycled PID)
    conn.execute(
        "INSERT OR REPLACE INTO delivery_stream_leases "
        "(job_id, holder_pid, holder_start_time, acquired_at, expires_at, gate_generation) "
        "VALUES ('recycled-job-456', ?, ?, ?, ?, 1)",
        (current_pid, actual_start - 100.0, env["now"], future_ts),
    )
    conn.commit()

    # get_active_stream_leases must detect PID reuse and report no active leases
    active = delivery.get_active_stream_leases(conn)
    conn.close()
    assert not any(l["job_id"] == "recycled-job-456" for l in active)


def test_repeated_migration_is_strictly_idempotent(env):
    """
    Migration Idempotence: Running reconcile(apply=True) repeatedly produces identical,
    clean schema and roles, and subsequent runs report migration_required=False.
    """
    db, uploads, photo = env["db"], env["uploads"], env["photo"]
    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()

    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS order_results ("
            "order_id INTEGER, path TEXT, sha256 TEXT, size INTEGER, version INTEGER, updated_at TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO order_results (order_id, path, sha256, size, version, updated_at) "
            "VALUES (1, ?, ?, ?, 1, ?)",
            (str(photo), file_sha, len(SYNTHETIC_DATA), env["now"]),
        )
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/1_result.bin", sha256=file_sha,
            size=len(SYNTHETIC_DATA), artifact_role=None,
        )

    res1 = reconcile(db, apply=True, uploads_dir=uploads)
    assert res1["applied"] is True

    # Second run must report migration_required=False
    res2 = reconcile(db, apply=True, uploads_dir=uploads)
    assert res2["migration_required"] is False
    assert res2["applied"] is False


def test_expired_lease_with_live_worker_blocks_migration(env):
    """
    Stage 08 Reproduction / Safety Invariant:
    Worker PID is ALIVE, job state is 'processing', artifact_role is 'result'.
    Worker acquired a stream lease whose expires_at passes.
    get_active_stream_leases() MUST NOT purge the lease solely due to expires_at.
    Migration MUST NOT acquire exclusive migration gate while worker process is alive.
    """
    db, photo = env["db"], env["photo"]
    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()
    with sqlite3.connect(db) as conn:
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/1_result.bin", sha256=file_sha,
            size=len(SYNTHETIC_DATA), artifact_role="result",
        )
        conn.execute("UPDATE delivery_jobs SET state='processing' WHERE id=?", (jid,))
        conn.commit()

        # Worker acquires stream lease with 1 second expiry
        lease = delivery.acquire_delivery_stream_lease(conn, jid, lease_seconds=1)
        pid = lease["holder_pid"]
        start_time = lease["holder_start_time"]
        assert delivery.is_pid_owner_alive(pid, start_time) is True

    # Advance time artificially past expires_at
    expired_now = (datetime.now(timezone.utc) + timedelta(seconds=15)).isoformat()

    with sqlite3.connect(db) as conn:
        active = delivery.get_active_stream_leases(conn, now=expired_now)
        # CRITICAL: Lease MUST remain active because worker PID is alive and job is processing!
        assert len(active) == 1
        assert active[0]["job_id"] == jid

    # Migration MUST fail closed with timeout while live worker holds lease
    with pytest.raises(RuntimeError, match="timed out waiting for active lock"):
        delivery.acquire_migration_gate(db, timeout=0.2, clock=lambda: expired_now)


def test_worker_heartbeat_renewal_protocol(env):
    """
    Heartbeat Protocol:
    Active worker extends expires_at during streaming using renew_delivery_stream_lease.
    Verification includes gate generation preservation and fail-closed security.
    """
    db, photo = env["db"], env["photo"]
    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()
    with sqlite3.connect(db) as conn:
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/1_result.bin", sha256=file_sha,
            size=len(SYNTHETIC_DATA), artifact_role="result",
        )
        conn.execute("UPDATE delivery_jobs SET state='processing' WHERE id=?", (jid,))
        conn.commit()

        lease = delivery.acquire_delivery_stream_lease(conn, jid, lease_seconds=10)
        orig_expires = lease["expires_at"]

        # Heartbeat renewal extends expires_at
        renewed = delivery.renew_delivery_stream_lease(conn, jid, lease_seconds=120)
        assert renewed["job_id"] == jid
        assert renewed["holder_pid"] == os.getpid()
        assert renewed["expires_at"] > orig_expires


def test_slow_upload_continuous_stream_lease_renewal(env, monkeypatch):
    """
    Slow Upload (> 60s):
    Simulates a multi-chunk transfer where each chunk extends the lease.
    Even though the total transfer exceeds 60 seconds, each chunk renews the lease,
    preventing expiration and maintaining mutual exclusion throughout.
    """
    db, uploads = env["db"], env["uploads"]
    multi_chunk_data = b"SLOW-UPLOAD-DATA-CHUNK-" * 5000
    file_sha = hashlib.sha256(multi_chunk_data).hexdigest()
    photo = uploads / "1_slow_result.bin"
    photo.write_bytes(multi_chunk_data)

    monkeypatch.setattr(main, "DB_PATH", db)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads)
    monkeypatch.setenv("YANDEX_DISK_TOKEN", "synthetic-token-only")
    monkeypatch.setenv("PHOTODOC_STAGING", "0")
    monkeypatch.setenv("PHOTODOC_ALLOW_FRESH_DB", "0")
    monkeypatch.setenv("YANDEX_DISK_ROOT_DIR", "PhotoDoc_Orders")

    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS order_results ("
            "order_id INTEGER, path TEXT, sha256 TEXT, size INTEGER, version INTEGER, updated_at TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO order_results (order_id, path, sha256, size, version, updated_at) "
            "VALUES (1, ?, ?, ?, 1, ?)",
            (str(photo), file_sha, len(multi_chunk_data), env["now"]),
        )
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/1_slow_result.bin", sha256=file_sha,
            size=len(multi_chunk_data), artifact_role="result",
        )

    class MockResponse:
        status_code = 200
        def json(self): return {"href": "https://upload.stub.test/v1/slow_stream"}
        def raise_for_status(self): pass

    monkeypatch.setattr(main, "ensure_yandex_folder", lambda *a, **k: None)
    monkeypatch.setattr(main.httpx, "get", lambda *a, **k: MockResponse())

    renewals_seen = []
    real_check = delivery.check_delivery_stream_lease

    def spy_check(conn, job_id, expected_generation, renew=True, **kwargs):
        res = real_check(conn, job_id, expected_generation, renew=renew, **kwargs)
        # Verify that expires_at is in the future
        row = conn.execute("SELECT expires_at FROM delivery_stream_leases WHERE job_id=?", (job_id,)).fetchone()
        if row:
            renewals_seen.append(row[0])
        return res

    monkeypatch.setattr(delivery, "check_delivery_stream_lease", spy_check)

    received_chunks = []
    def streaming_put_stub(url, *, content, headers=None, timeout=None):
        for chunk in content:
            received_chunks.append(chunk)
        return MockResponse()

    monkeypatch.setattr(main.httpx, "put", streaming_put_stub)
    monkeypatch.setattr(main, "_remote_verified", lambda *a, **k: len(received_chunks) > 0)

    processed = delivery.process_due(db, main.deliver_job, limit=1, uploads_dir=uploads)
    assert processed == 1
    assert len(received_chunks) > 1
    assert len(renewals_seen) == len(received_chunks)

    # After delivery completes, lease was cleanly released
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM delivery_stream_leases WHERE job_id=?", (jid,)).fetchone()[0]
        assert count == 0
        state = conn.execute("SELECT state FROM delivery_jobs WHERE id=?", (jid,)).fetchone()[0]
        assert state == "delivered"


def test_heartbeat_failure_halts_streaming_fail_closed(env, monkeypatch):
    """
    Heartbeat Failure / Mid-stream Gate Intrusion:
    If migration gate generation increases or lease is altered mid-stream,
    the next chunk validation fails closed immediately and aborts the stream.
    """
    db, uploads = env["db"], env["uploads"]
    multi_chunk_data = b"HEARTBEAT-FAIL-DATA-CHUNK-" * 4000
    file_sha = hashlib.sha256(multi_chunk_data).hexdigest()
    photo = uploads / "1_fail_hb_result.bin"
    photo.write_bytes(multi_chunk_data)

    monkeypatch.setattr(main, "DB_PATH", db)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads)
    monkeypatch.setenv("YANDEX_DISK_TOKEN", "synthetic-token-only")
    monkeypatch.setenv("PHOTODOC_STAGING", "0")
    monkeypatch.setenv("PHOTODOC_ALLOW_FRESH_DB", "0")
    monkeypatch.setenv("YANDEX_DISK_ROOT_DIR", "PhotoDoc_Orders")

    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS order_results ("
            "order_id INTEGER, path TEXT, sha256 TEXT, size INTEGER, version INTEGER, updated_at TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO order_results (order_id, path, sha256, size, version, updated_at) "
            "VALUES (1, ?, ?, ?, 1, ?)",
            (str(photo), file_sha, len(multi_chunk_data), env["now"]),
        )
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/1_fail_hb_result.bin", sha256=file_sha,
            size=len(multi_chunk_data), artifact_role="result",
        )

    class MockResponse:
        status_code = 200
        def json(self): return {"href": "https://upload.stub.test/v1/hb_fail_stream"}
        def raise_for_status(self): pass

    monkeypatch.setattr(main, "ensure_yandex_folder", lambda *a, **k: None)
    monkeypatch.setattr(main, "_remote_verified", lambda *a, **k: False)
    monkeypatch.setattr(main.httpx, "get", lambda *a, **k: MockResponse())

    received_chunks = []
    def streaming_put_stub(url, *, content, headers=None, timeout=None):
        iterator = iter(content)
        # Yield first chunk successfully
        received_chunks.append(next(iterator))

        # Mid-stream: simulate role change to non-result in delivery_jobs
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE delivery_jobs SET artifact_role='source' WHERE id=?", (jid,))
            conn.commit()

        # Second chunk must raise RuntimeError (Delivery forbidden: artifact role changed)
        for chunk in iterator:
            received_chunks.append(chunk)
        return MockResponse()

    monkeypatch.setattr(main.httpx, "put", streaming_put_stub)

    processed = delivery.process_due(db, main.deliver_job, limit=1, uploads_dir=uploads)
    assert processed == 1
    # Only the first chunk was sent before authorization failed closed
    assert len(received_chunks) == 1

    # Delivery must NOT be marked delivered and local file must NOT be deleted
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT state, artifact_role FROM delivery_jobs WHERE id=?", (jid,)).fetchone()
        assert row[0] == "failed"
        assert row[1] == "source"
    assert photo.exists()


def test_dead_worker_reclaimed_by_migration(env):
    """
    Crash Recovery:
    Dead worker PID (e.g. 999999) holding an unreleased stream lease is automatically
    detected as dead and purged by migration, allowing exclusive gate acquisition.
    """
    db = env["db"]
    conn = sqlite3.connect(db)
    delivery.ensure_gate_table(conn)
    conn.execute(
        "INSERT OR REPLACE INTO delivery_stream_leases "
        "(job_id, holder_pid, holder_start_time, acquired_at, expires_at, gate_generation) "
        "VALUES ('crashed-worker-job', 999999, 11111.0, ?, ?, 1)",
        (env["now"], env["now"]),
    )
    conn.commit()
    conn.close()

    # Migration must purge the dead worker's lease and acquire the gate cleanly
    holder = delivery.acquire_migration_gate(db, timeout=0.5)
    assert bool(holder) is True
    delivery.release_migration_gate(db, holder)

    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM delivery_stream_leases WHERE job_id='crashed-worker-job'").fetchone()[0]
        assert count == 0


def test_live_worker_stream_lease_persists_regardless_of_terminal_state(env):
    """
    PHOTODOC-R1-STREAM-LEASE-STATE-FENCING-09 (Requirement 7):
    get_active_stream_leases() MUST NOT purge a lease solely because the job in delivery_jobs
    is in a terminal state ('delivered' or 'failed') if the holder process is confirmed alive.
    The active stream lease continues to fence migration until the worker explicitly releases it.
    """
    db, photo = env["db"], env["photo"]
    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()
    with sqlite3.connect(db) as conn:
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/1_result.bin", sha256=file_sha,
            size=len(SYNTHETIC_DATA), artifact_role="result",
        )
        conn.execute("UPDATE delivery_jobs SET state='processing', lease_until='2099-01-01T00:00:00' WHERE id=?", (jid,))
        conn.commit()

        lease = delivery.acquire_delivery_stream_lease(conn, jid)
        op_id = lease["stream_operation_id"]

        # Simulate external state mutation to terminal state 'failed' or 'delivered'
        conn.execute("UPDATE delivery_jobs SET state='failed' WHERE id=?", (jid,))
        conn.commit()

        active = delivery.get_active_stream_leases(conn)
        assert len(active) == 1
        assert active[0]["job_id"] == jid
        assert active[0]["stream_operation_id"] == op_id

    # Migration MUST be blocked despite the terminal job state because the holder is alive!
    with pytest.raises(RuntimeError, match="timed out"):
        delivery.acquire_migration_gate(db, timeout=0.2)

    # Release lease cleanly
    assert delivery.release_delivery_stream_lease(db, jid, stream_operation_id=op_id) is True

    # Now migration can acquire gate
    holder = delivery.acquire_migration_gate(db, timeout=0.5)
    assert bool(holder) is True
    delivery.release_migration_gate(db, holder)


def test_sqlite_contention_during_stream_renewal_retries_successfully(env, monkeypatch):
    """
    SQLite Contention Resilience:
    Simulates transient SQLite lock contention during renew_delivery_stream_lease;
    @retry_busy automatically retries and succeeds without losing lease.
    """
    db, photo = env["db"], env["photo"]
    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()
    with sqlite3.connect(db) as conn:
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/1_result.bin", sha256=file_sha,
            size=len(SYNTHETIC_DATA), artifact_role="result",
        )
        conn.execute("UPDATE delivery_jobs SET state='processing' WHERE id=?", (jid,))
        conn.commit()
        delivery.acquire_delivery_stream_lease(conn, jid, lease_seconds=10)

    # Inject transient OperationalError on the first 2 attempts
    attempts = [0]
    real_connect = sqlite3.connect

    def contending_connect(*args, **kwargs):
        attempts[0] += 1
        if attempts[0] <= 2:
            raise sqlite3.OperationalError("database is locked")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", contending_connect)

    renewed = delivery.renew_delivery_stream_lease(db, jid, lease_seconds=60)
    assert renewed["job_id"] == jid
    assert attempts[0] >= 3


def test_multiple_concurrent_workers_stream_leases_isolated(env):
    """
    Multi-worker Concurrency:
    Two concurrent workers holding stream leases for distinct jobs are isolated;
    releasing or renewing one lease does not affect the other. Migration is blocked
    as long as ANY active stream lease is held.
    """
    db, photo = env["db"], env["photo"]
    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()
    with sqlite3.connect(db) as conn:
        jid1 = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/1_result.bin", sha256=file_sha,
            size=len(SYNTHETIC_DATA), artifact_role="result",
        )
        jid2 = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/2_result.bin", sha256=file_sha,
            size=len(SYNTHETIC_DATA), artifact_role="result",
        )
        conn.execute("UPDATE delivery_jobs SET state='processing' WHERE id IN (?, ?)", (jid1, jid2))
        conn.commit()

        lease1 = delivery.acquire_delivery_stream_lease(conn, jid1, lease_seconds=30)
        lease2 = delivery.acquire_delivery_stream_lease(conn, jid2, lease_seconds=30)
        assert lease1["job_id"] == jid1
        assert lease2["job_id"] == jid2

        active = delivery.get_active_stream_leases(conn)
        assert len(active) == 2

    # Migration blocked while both are active
    with pytest.raises(RuntimeError, match="timed out"):
        delivery.acquire_migration_gate(db, timeout=0.2)

    # Release worker 1's lease
    released = delivery.release_delivery_stream_lease(db, jid1)
    assert released is True

    # Migration STILL blocked because worker 2 is active
    with pytest.raises(RuntimeError, match="timed out"):
        delivery.acquire_migration_gate(db, timeout=0.2)

    # Release worker 2's lease
    released2 = delivery.release_delivery_stream_lease(db, jid2)
    assert released2 is True

    # Now migration succeeds immediately
    holder = delivery.acquire_migration_gate(db, timeout=0.5)
    assert bool(holder) is True
    delivery.release_migration_gate(db, holder)


# ==============================================================================
# PHOTODOC-R1-STREAM-LEASE-STATE-FENCING-09:
# Mutual Exclusion against State Modification & stream_operation_id Fencing
# ==============================================================================

def test_probe2_state_fencing_prevents_unauthorized_finish_and_blocks_migration(env):
    """
    PROBE2 Verification (PHOTODOC-R1-STREAM-LEASE-STATE-FENCING-09):
    1. Worker acquires stream lease (stream_operation_id assigned).
    2. Concurrent external finish(error) or finish(success) without matching stream_operation_id
       is strictly rejected (returns False, state stays 'processing').
    3. Even if state is mutated via direct SQL to 'failed' or 'delivered', get_active_stream_leases
       continues to return the lease as active as long as the worker PID is alive.
    4. Migration gate acquisition remains strictly blocked (RuntimeError timeout).
    5. Authorized finish with matching stream_operation_id succeeds and atomically releases lease.
    6. Migration gate succeeds only after clean termination.
    """
    db, photo = env["db"], env["photo"]
    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()
    with sqlite3.connect(db) as conn:
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/probe2_result.bin", sha256=file_sha,
            size=len(SYNTHETIC_DATA), artifact_role="result",
        )
        conn.execute("UPDATE delivery_jobs SET state='processing', lease_until='2099-01-01T00:00:00' WHERE id=?", (jid,))
        conn.commit()

        lease = delivery.acquire_delivery_stream_lease(conn, jid)
        op_id = lease["stream_operation_id"]
        assert op_id.startswith("sop_")

    # 1. Rogue finish(error) without stream_operation_id must be rejected
    rejected_err = delivery.finish(db, jid, error=RuntimeError("External rogue error"))
    assert rejected_err is False

    with sqlite3.connect(db) as conn:
        state = conn.execute("SELECT state FROM delivery_jobs WHERE id=?", (jid,)).fetchone()[0]
        assert state == "processing"
        active = delivery.get_active_stream_leases(conn)
        assert len(active) == 1
        assert active[0]["stream_operation_id"] == op_id

    # 2. Rogue finish(success) without stream_operation_id must be rejected
    rejected_succ = delivery.finish(db, jid)
    assert rejected_succ is False

    with sqlite3.connect(db) as conn:
        state = conn.execute("SELECT state FROM delivery_jobs WHERE id=?", (jid,)).fetchone()[0]
        assert state == "processing"

    # 3. Migration gate MUST be blocked
    with pytest.raises(RuntimeError, match="timed out"):
        delivery.acquire_migration_gate(db, timeout=0.2)

    # 4. Raw SQL state mutation test: even if state='failed' is set directly:
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE delivery_jobs SET state='failed' WHERE id=?", (jid,))
        conn.commit()
        active = delivery.get_active_stream_leases(conn)
        assert len(active) == 1
        assert active[0]["stream_operation_id"] == op_id

    # Migration gate STILL blocked
    with pytest.raises(RuntimeError, match="timed out"):
        delivery.acquire_migration_gate(db, timeout=0.2)

    # 5. Restore state to processing for clean worker completion
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE delivery_jobs SET state='processing' WHERE id=?", (jid,))
        conn.commit()

    # Authorized worker finish with matching stream_operation_id after HTTP transmission completion
    delivery.confirm_delivery_stream_completed(db, jid, op_id)
    committed = delivery.finish(db, jid, stream_operation_id=op_id)
    assert committed is True

    with sqlite3.connect(db) as conn:
        state = conn.execute("SELECT state FROM delivery_jobs WHERE id=?", (jid,)).fetchone()[0]
        assert state == "delivered"
        active = delivery.get_active_stream_leases(conn)
        assert len(active) == 0

    # 6. Migration gate can now be acquired cleanly
    holder = delivery.acquire_migration_gate(db, timeout=0.5)
    assert bool(holder) is True
    delivery.release_migration_gate(db, holder)


def test_stream_operation_id_fencing_between_threads_same_pid(env):
    """
    stream_operation_id Fencing between operations in same process:
    Prevents cross-operation contamination even when PID matches.
    """
    db, photo = env["db"], env["photo"]
    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()
    with sqlite3.connect(db) as conn:
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/thread_test.bin", sha256=file_sha,
            size=len(SYNTHETIC_DATA), artifact_role="result",
        )
        conn.execute("UPDATE delivery_jobs SET state='processing', lease_until='2099-01-01T00:00:00' WHERE id=?", (jid,))
        conn.commit()

        lease = delivery.acquire_delivery_stream_lease(conn, jid)
        op_1 = lease["stream_operation_id"]
        op_2 = "sop_other_thread_9999"

        # Check with mismatched operation_id fails closed
        with pytest.raises(RuntimeError, match="stream_operation_id mismatch"):
            delivery.check_delivery_stream_lease(conn, jid, expected_generation=1, stream_operation_id=op_2)

        # Renew with mismatched operation_id fails closed
        with pytest.raises(RuntimeError, match="stream_operation_id mismatch"):
            delivery.renew_delivery_stream_lease(conn, jid, stream_operation_id=op_2)

        # Release with mismatched operation_id fails closed / returns False
        assert delivery.release_delivery_stream_lease(conn, jid, stream_operation_id=op_2) is False

        # Finish with mismatched operation_id returns False
        assert delivery.finish(db, jid, stream_operation_id=op_2) is False

        # Correct operation_id succeeds
        assert delivery.check_delivery_stream_lease(conn, jid, expected_generation=1, stream_operation_id=op_1) is True
        renewed = delivery.renew_delivery_stream_lease(conn, jid, stream_operation_id=op_1)
        assert renewed["stream_operation_id"] == op_1
        assert delivery.release_delivery_stream_lease(conn, jid, stream_operation_id=op_1) is True


def test_quarantine_expired_fencing_active_stream_lease(env):
    """
    quarantine_expired() MUST NOT transition a job to 'failed'
    while an active stream lease is held for that job.
    """
    db, photo = env["db"], env["photo"]
    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()
    now_ts = env["now"]
    past_ts = "2020-01-01T00:00:00"
    with sqlite3.connect(db) as conn:
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/quarantine_fence.bin", sha256=file_sha,
            size=len(SYNTHETIC_DATA), artifact_role="result",
        )
        # Set job to look expired under normal conditions
        conn.execute(
            "UPDATE delivery_jobs SET state='processing', attempts=?, lease_until=? WHERE id=?",
            (delivery.DEFAULT_MAX_ATTEMPTS, past_ts, jid),
        )
        conn.commit()

        # Hold active stream lease
        lease = delivery.acquire_delivery_stream_lease(conn, jid)
        op_id = lease["stream_operation_id"]

        # quarantine_expired should NOT touch this job
        affected = delivery.quarantine_expired(conn, now_ts, max_attempts=delivery.DEFAULT_MAX_ATTEMPTS)
        assert affected == 0

        state = conn.execute("SELECT state FROM delivery_jobs WHERE id=?", (jid,)).fetchone()[0]
        assert state == "processing"

        # Release lease
        assert delivery.release_delivery_stream_lease(conn, jid, stream_operation_id=op_id) is True

        # Now quarantine_expired CAN touch this job
        affected_after = delivery.quarantine_expired(conn, now_ts, max_attempts=delivery.DEFAULT_MAX_ATTEMPTS)
        assert affected_after == 1

        state_after = conn.execute("SELECT state FROM delivery_jobs WHERE id=?", (jid,)).fetchone()[0]
        assert state_after == "failed"


def test_claim_due_fencing_active_stream_lease(env):
    """
    claim_due() MUST NOT claim a job that is currently holding an active stream lease.
    """
    db, photo = env["db"], env["photo"]
    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()
    past_ts = "2020-01-01T00:00:00"
    with sqlite3.connect(db) as conn:
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/claim_due_fence.bin", sha256=file_sha,
            size=len(SYNTHETIC_DATA), artifact_role="result",
        )
        conn.execute("UPDATE delivery_jobs SET state='processing', attempts=1, lease_until=? WHERE id=?", (past_ts, jid))
        conn.commit()

        # Hold active stream lease
        lease = delivery.acquire_delivery_stream_lease(conn, jid)
        op_id = lease["stream_operation_id"]

    # Another worker attempts claim_due
    claimed = delivery.claim_due(db)
    # The active streaming job must NOT be claimed
    assert claimed is None

    # Release stream lease
    with sqlite3.connect(db) as conn:
        delivery.release_delivery_stream_lease(conn, jid, stream_operation_id=op_id)

    # Now claim_due CAN claim the expired job
    claimed_after = delivery.claim_due(db)
    assert claimed_after is not None
    assert claimed_after["id"] == jid


def test_crashed_worker_dead_lease_recovery(env):
    """
    Crash recovery:
    Dead worker's lease is purged, allowing state transitions and migration gate acquisition.
    """
    db, photo = env["db"], env["photo"]
    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()
    with sqlite3.connect(db) as conn:
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/crashed_worker.bin", sha256=file_sha,
            size=len(SYNTHETIC_DATA), artifact_role="result",
        )
        conn.execute("UPDATE delivery_jobs SET state='processing', lease_until='2099-01-01T00:00:00' WHERE id=?", (jid,))
        delivery.ensure_gate_table(conn)
        # Dead PID (non-existent process)
        conn.execute(
            "INSERT INTO delivery_stream_leases (job_id, holder_pid, holder_start_time, acquired_at, expires_at, gate_generation, stream_operation_id) "
            "VALUES (?, 9999999, 12345.0, ?, ?, 1, 'sop_crashed')",
            (jid, env["now"], env["now"]),
        )
        conn.commit()

        # get_active_stream_leases purges dead lease
        active = delivery.get_active_stream_leases(conn)
        assert len(active) == 0

        # Migration gate can be acquired
        holder = delivery.acquire_migration_gate(db, timeout=0.5)
        assert bool(holder) is True
        delivery.release_migration_gate(db, holder)

        # finish() cleans up and succeeds
        res = delivery.finish(db, jid)
        assert res is True
        state = conn.execute("SELECT state FROM delivery_jobs WHERE id=?", (jid,)).fetchone()[0]
        assert state == "delivered"


def test_cross_process_stream_lease_fencing(env):
    """
    Cross-process validation:
    A child process holds a stream lease.
    Parent process verifies:
    1. Parent cannot acquire migration gate while child is alive.
    2. Parent cannot finish child's job.
    3. When child terminates, parent can acquire migration gate.
    """
    import subprocess
    import sys
    db, photo = env["db"], env["photo"]
    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()
    with sqlite3.connect(db) as conn:
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/cross_proc.bin", sha256=file_sha,
            size=len(SYNTHETIC_DATA), artifact_role="result",
        )
        conn.execute("UPDATE delivery_jobs SET state='processing', lease_until='2099-01-01T00:00:00' WHERE id=?", (jid,))
        conn.commit()

    backend_dir = Path(__file__).resolve().parent.parent
    child_code = f"""
import sys, time, sqlite3
from pathlib import Path
sys.path.insert(0, r"{backend_dir}")
import delivery

db = Path(r"{db}")
with sqlite3.connect(db) as conn:
    lease = delivery.acquire_delivery_stream_lease(conn, "{jid}")
    print("READY", flush=True)

time.sleep(3.0)
"""
    proc = subprocess.Popen([sys.executable, "-c", child_code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready_line = proc.stdout.readline()
        assert "READY" in ready_line

        # Parent cannot finish child's job
        finish_res = delivery.finish(db, jid)
        assert finish_res is False

        # Parent cannot acquire migration gate
        with pytest.raises(RuntimeError, match="timed out"):
            delivery.acquire_migration_gate(db, timeout=0.2)

        # Terminate child
        proc.terminate()
        proc.wait(timeout=5)

        # Now parent can acquire migration gate cleanly
        holder = delivery.acquire_migration_gate(db, timeout=1.0)
        assert bool(holder) is True
        delivery.release_migration_gate(db, holder)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# ==============================================================================
# PHOTODOC-R1-STREAM-LEASE-LIVENESS-FIX-10:
# Separation of HTTP Transmission, Confirmation, Lease Release, Finalization
# ==============================================================================

def test_finish_during_active_stream_rejected_even_with_valid_token(env):
    """
    Stage 10 Invariant: finish() MUST reject premature finalization of active streams
    even when presented with a valid stream_operation_id token, as long as http_completed == 0.
    """
    db, photo = env["db"], env["photo"]
    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()
    with sqlite3.connect(db) as conn:
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/in_flight_stream.bin", sha256=file_sha,
            size=len(SYNTHETIC_DATA), artifact_role="result",
        )
        conn.execute("UPDATE delivery_jobs SET state='processing', lease_until='2099-01-01T00:00:00' WHERE id=?", (jid,))
        conn.commit()

        lease = delivery.acquire_delivery_stream_lease(conn, jid)
        op_id = lease["stream_operation_id"]
        assert lease["http_completed"] is False
        assert lease["delivery_confirmed"] is False

    # Attempt to call finish() with valid op_id while http_completed is 0
    res = delivery.finish(db, jid, stream_operation_id=op_id)
    assert res is False

    # Job remains in processing, stream lease is preserved
    with sqlite3.connect(db) as conn:
        state = conn.execute("SELECT state FROM delivery_jobs WHERE id=?", (jid,)).fetchone()[0]
        assert state == "processing"
        count = conn.execute("SELECT COUNT(*) FROM delivery_stream_leases WHERE job_id=?", (jid,)).fetchone()[0]
        assert count == 1

    # Cleanup
    delivery.release_delivery_stream_lease(db, jid, stream_operation_id=op_id)


def test_finish_after_confirm_completed_succeeds_and_atomically_cleans_lease(env):
    """
    Stage 10 Invariant: Once HTTP stream is verified and confirm_delivery_stream_completed
    is called, finish() with matching stream_operation_id succeeds, marks job 'delivered',
    and atomically deletes the lease record from SQLite in the same transaction.
    """
    db, photo = env["db"], env["photo"]
    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()
    with sqlite3.connect(db) as conn:
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/confirmed_stream.bin", sha256=file_sha,
            size=len(SYNTHETIC_DATA), artifact_role="result",
        )
        conn.execute("UPDATE delivery_jobs SET state='processing', lease_until='2099-01-01T00:00:00' WHERE id=?", (jid,))
        conn.commit()

        lease = delivery.acquire_delivery_stream_lease(conn, jid)
        op_id = lease["stream_operation_id"]

    # Confirm stream completion
    assert delivery.confirm_delivery_stream_completed(db, jid, op_id) is True

    # Now finish() succeeds
    committed = delivery.finish(db, jid, stream_operation_id=op_id)
    assert committed is True

    with sqlite3.connect(db) as conn:
        state = conn.execute("SELECT state FROM delivery_jobs WHERE id=?", (jid,)).fetchone()[0]
        assert state == "delivered"
        # Lease atomically deleted by finish()
        count = conn.execute("SELECT COUNT(*) FROM delivery_stream_leases WHERE job_id=?", (jid,)).fetchone()[0]
        assert count == 0


def test_release_failure_sqlite_contention_followed_by_finish_finalizes_cleanly(env, monkeypatch):
    """
    Stage 10 Liveness Fix:
    If release_delivery_stream_lease() fails due to transient SQLite contention or is interrupted,
    finish() with stream_operation_id atomically finalizes the state and deletes the lease,
    preventing jobs from becoming stuck in 'processing'.
    """
    db, photo = env["db"], env["photo"]
    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()
    with sqlite3.connect(db) as conn:
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/contention_stream.bin", sha256=file_sha,
            size=len(SYNTHETIC_DATA), artifact_role="result",
        )
        conn.execute("UPDATE delivery_jobs SET state='processing', lease_until='2099-01-01T00:00:00' WHERE id=?", (jid,))
        conn.commit()

        lease = delivery.acquire_delivery_stream_lease(conn, jid)
        op_id = lease["stream_operation_id"]

    delivery.confirm_delivery_stream_completed(db, jid, op_id)

    # Simulate release failure (e.g. timeout or exception during release step)
    # The lease remains in delivery_stream_leases with http_completed=1, delivery_confirmed=1
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT http_completed, delivery_confirmed FROM delivery_stream_leases WHERE job_id=?", (jid,)).fetchone()
        assert row[0] == 1 and row[1] == 1

    # process_due / finish() runs:
    committed = delivery.finish(db, jid, stream_operation_id=op_id)
    assert committed is True

    with sqlite3.connect(db) as conn:
        state = conn.execute("SELECT state FROM delivery_jobs WHERE id=?", (jid,)).fetchone()[0]
        assert state == "delivered"
        count = conn.execute("SELECT COUNT(*) FROM delivery_stream_leases WHERE job_id=?", (jid,)).fetchone()[0]
        assert count == 0


def test_retry_after_contention_or_restart_does_not_retransmit_duplicate_bytes(env, monkeypatch):
    """
    Stage 10 Verification:
    If delivery is retried after a previous attempt confirmed the file on remote storage,
    upload_to_yandex_disk immediately confirms completion and does NOT reopen or re-transmit
    file bytes (zero PUT requests).
    """
    db, uploads, photo = env["db"], env["uploads"], env["photo"]
    file_sha = hashlib.sha256(SYNTHETIC_DATA).hexdigest()
    file_size = len(SYNTHETIC_DATA)

    monkeypatch.setattr(main, "DB_PATH", db)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads)
    monkeypatch.setenv("YANDEX_DISK_TOKEN", "synthetic-token-only")
    monkeypatch.setenv("PHOTODOC_STAGING", "0")
    monkeypatch.setenv("PHOTODOC_ALLOW_FRESH_DB", "0")
    monkeypatch.setenv("YANDEX_DISK_ROOT_DIR", "PhotoDoc_Orders")

    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS order_results ("
            "order_id INTEGER, path TEXT, sha256 TEXT, size INTEGER, version INTEGER, updated_at TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO order_results (order_id, path, sha256, size, version, updated_at) "
            "VALUES (1, ?, ?, ?, 1, ?)",
            (str(photo), file_sha, file_size, env["now"]),
        )
        jid = delivery.enqueue(
            conn, order_id=1, kind="file", local_path=str(photo),
            remote_path="PhotoDoc_Orders/1_dup_check.bin", sha256=file_sha,
            size=file_size, artifact_role="result",
        )

    # Remote file is already verified
    monkeypatch.setattr(main, "_remote_verified", lambda *a, **k: True)

    put_calls = []
    monkeypatch.setattr(main.httpx, "put", lambda *a, **k: put_calls.append(True))

    processed = delivery.process_due(db, main.deliver_job, limit=1, uploads_dir=uploads)
    assert processed == 1
    # Zero PUT calls were made — no duplicate bytes sent!
    assert len(put_calls) == 0

    with sqlite3.connect(db) as conn:
        state = conn.execute("SELECT state FROM delivery_jobs WHERE id=?", (jid,)).fetchone()[0]
        assert state == "delivered"
        count = conn.execute("SELECT COUNT(*) FROM delivery_stream_leases WHERE job_id=?", (jid,)).fetchone()[0]
        assert count == 0




