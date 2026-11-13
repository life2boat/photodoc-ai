"""Comprehensive tests for H-02: Retention background worker and application lifecycle."""
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import delivery
import main
import paid_hold
import retention
import retention_worker
from main import app


@pytest.fixture
def env(tmp_path, monkeypatch):
    test_db = tmp_path / "orders.db"
    test_uploads = tmp_path / "uploads"
    test_uploads.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(main, "DB_PATH", test_db)
    monkeypatch.setattr(main, "UPLOAD_DIR", test_uploads)
    delivery.set_uploads_dir(test_uploads)
    main.init_db()

    monkeypatch.setenv("DELIVERY_WORKER_ENABLED", "0")
    monkeypatch.setenv("RETENTION_WORKER_ENABLED", "0")

    yield {"db": test_db, "uploads": test_uploads}


def test_retention_safety_validations(env, monkeypatch):
    # RETENTION_HOURS < 24 must fail closed
    with pytest.raises(RuntimeError, match="must be >= 24"):
        retention_worker.validate_retention_safety(env["db"], env["uploads"], ttl_hours=12)

    # Missing uploads directory must fail closed
    missing_dir = env["uploads"] / "does_not_exist"
    with pytest.raises(RuntimeError, match="uploads directory does not exist"):
        retention_worker.validate_retention_safety(env["db"], missing_dir, ttl_hours=72)


def test_multi_instance_lease_fencing(env):
    with sqlite3.connect(env["db"]) as conn:
        inst1 = "worker-node-1"
        inst2 = "worker-node-2"

        # Node 1 acquires lease for 60s
        acquired1 = retention_worker.acquire_retention_lease(
            conn, inst1, lease_duration_seconds=60
        )
        assert acquired1 is True

        # Node 2 cannot acquire while Node 1 holds active lease
        acquired2 = retention_worker.acquire_retention_lease(
            conn, inst2, lease_duration_seconds=60
        )
        assert acquired2 is False

        # Node 1 can renew its own lease
        renewed1 = retention_worker.acquire_retention_lease(
            conn, inst1, lease_duration_seconds=60
        )
        assert renewed1 is True

        # Node 1 releases lease
        retention_worker.release_retention_lease(conn, inst1)

        # Now Node 2 can acquire lease
        acquired2_after_release = retention_worker.acquire_retention_lease(
            conn, inst2, lease_duration_seconds=60
        )
        assert acquired2_after_release is True


def test_retention_cycle_purges_unpaid_and_escalates_paid_without_deletion(env):
    db_path = env["db"]
    uploads = env["uploads"]
    now = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Unpaid order created 80 hours ago (should be purged by retention)
    unpaid_file = uploads / "101_unpaid.jpg"
    unpaid_file.write_bytes(b"\xff\xd8\xff\xe0" + b"UNPAID-SOURCE-DATA" * 4)
    created_80h = (now - timedelta(hours=80)).isoformat()

    # 2. Paid order created 8 days ago (should be escalated, NOT deleted!)
    paid_file = uploads / "102_paid.jpg"
    paid_file.write_bytes(b"\xff\xd8\xff\xe0" + b"PAID-CUSTOMER-IMAGE" * 4)
    created_8d = (now - timedelta(days=8)).isoformat()

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO orders (id, name, phone, status, payment_status, created_at) VALUES "
            "(101, 'Unpaid User', '+7999111', 'new', 'unpaid', ?), "
            "(102, 'Paid User', '+7999222', 'new', 'paid', ?)",
            (created_80h, created_8d),
        )
        conn.execute(
            "INSERT INTO delivery_jobs (id, order_id, kind, local_path, original_name, remote_path, "
            "content_sha256, content_size, state, attempts, next_attempt_at, created_at, cleanup_state, cleanup_phase, artifact_role) VALUES "
            "('job-101', 101, 'file', ?, 'unpaid.jpg', 'disk/101.jpg', 'sha', 76, 'pending', 0, ?, ?, 'pending', 'pending', 'source'), "
            "('job-102', 102, 'file', ?, 'paid.jpg', 'disk/102.jpg', 'sha', 76, 'pending', 0, ?, ?, 'pending', 'pending', 'source')",
            (str(unpaid_file), created_80h, created_80h, str(paid_file), created_8d, created_8d),
        )
        conn.commit()

    # Run coordinated retention cycle
    summary = retention_worker.run_retention_cycle(
        db_path,
        uploads,
        instance_id="test-runner",
        now=now,
    )

    assert summary["ok"] is True
    # Unpaid file is purged
    assert not unpaid_file.exists()

    # Paid file is PROTECTED and MUST REMAIN ON DISK
    assert paid_file.exists(), "INVARIANT VIOLATION: Paid customer file was deleted!"

    # Verify escalation recorded in paid_hold_audit
    with sqlite3.connect(db_path) as conn:
        hold_row = conn.execute(
            "SELECT state, note FROM paid_hold_audit WHERE order_id=102"
        ).fetchone()
        assert hold_row is not None
        assert hold_row[0] == "escalated"

        # Verify telemetry was recorded
        telem = conn.execute(
            "SELECT purged_expired_count, holds_escalated FROM retention_telemetry"
        ).fetchone()
        assert telem is not None
        assert telem[0] >= 1
        assert telem[1] >= 1


def test_retention_worker_lifespan_integration(monkeypatch, env):
    monkeypatch.setenv("RETENTION_WORKER_ENABLED", "1")
    monkeypatch.setenv("RETENTION_WORKER_INTERVAL_SECONDS", "0.2")

    # Start and stop app lifespan using TestClient context
    with TestClient(app) as client:
        time.sleep(0.3)
        resp = client.get("/health")
        assert resp.status_code in (200, 404)  # Server responds

    # Verify clean shutdown: lease table exists and was released
    with sqlite3.connect(env["db"]) as conn:
        lease = conn.execute(
            "SELECT lease_until FROM retention_scheduler_lease WHERE lease_name='retention_worker'"
        ).fetchone()
        # When released, lease_until is set to epoch or expired
        if lease:
            assert "1970" in lease[0] or datetime.fromisoformat(lease[0]) <= datetime.now(timezone.utc)
