"""Comprehensive tests for M-03: Rotation safety checks and runtime enforcement."""
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup_rotation
import log_rotation
import main
from main import app, validate_rotation_safety


def test_backup_rotation_param_validation():
    # retention_days must be > 0
    with pytest.raises(ValueError, match="retention_days must be > 0"):
        backup_rotation.validate_backup_rotation_params(retention_days=0, keep_min=2)

    with pytest.raises(ValueError, match="retention_days must be > 0"):
        backup_rotation.validate_backup_rotation_params(retention_days=-1, keep_min=2)

    # keep_min must be >= 0
    with pytest.raises(ValueError, match="keep_min must be >= 0"):
        backup_rotation.validate_backup_rotation_params(retention_days=3, keep_min=-1)

    # max_age_days cannot be less than retention_days
    with pytest.raises(ValueError, match="cannot be less than retention_days"):
        backup_rotation.validate_backup_rotation_params(retention_days=7, keep_min=2, max_age_days=5)

    # Valid bounds succeed
    backup_rotation.validate_backup_rotation_params(retention_days=3, keep_min=2, max_age_days=30)
    backup_rotation.validate_backup_rotation_params(retention_days=1, keep_min=0, max_age_days=None)


def test_backup_dir_safety_validation(tmp_path):
    # Non-existent directory
    missing_dir = tmp_path / "non_existent"
    with pytest.raises(ValueError, match="does not exist or is not a directory"):
        backup_rotation.validate_backup_dir_safety(missing_dir)

    # Path traversal
    with pytest.raises(ValueError, match="Path traversal detected"):
        backup_rotation.validate_backup_dir_safety(tmp_path / ".." / "test")

    # Valid directory succeeds
    valid_dir = tmp_path / "backups"
    valid_dir.mkdir()
    resolved = backup_rotation.validate_backup_dir_safety(valid_dir)
    assert resolved == valid_dir.resolve()


def test_log_retention_policy_verification(tmp_path):
    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)

    # Fresh log: 2 days old
    fresh_log = tmp_path / "access.log.1"
    fresh_log.write_text("fresh log entry")
    mtime_fresh = (now - timedelta(days=2)).timestamp()
    os.utime(fresh_log, (mtime_fresh, mtime_fresh))

    # Expired log: 20 days old (violates 15-day policy)
    expired_log = tmp_path / "access.log.20"
    expired_log.write_text("old expired log entry")
    mtime_old = (now - timedelta(days=20)).timestamp()
    os.utime(expired_log, (mtime_old, mtime_old))

    # Verification run (dry_run=True)
    summary = log_rotation.verify_log_retention_policy(tmp_path, max_days=15, now=now, dry_run=True)
    assert summary["ok"] is False
    assert summary["scanned"] == 2
    assert summary["expired"] == 1
    assert summary["purged"] == 0
    assert summary["violations"][0]["file"] == "access.log.20"
    assert expired_log.exists()

    # Active enforcement run (dry_run=False)
    summary_purge = log_rotation.verify_log_retention_policy(tmp_path, max_days=15, now=now, dry_run=False)
    assert summary_purge["ok"] is False  # Had violations before purging
    assert summary_purge["purged"] == 1
    assert not expired_log.exists()
    assert fresh_log.exists()

    # Subsequent verification run passes
    clean_summary = log_rotation.verify_log_retention_policy(tmp_path, max_days=15, now=now, dry_run=True)
    assert clean_summary["ok"] is True
    assert clean_summary["expired"] == 0


def test_startup_validation_hook_enforces_backup_and_log_safety(tmp_path, monkeypatch):
    # Case 1: BACKUP_ROTATION_ENABLED=1 without BACKUP_DIR fails closed
    monkeypatch.setenv("BACKUP_ROTATION_ENABLED", "1")
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    with pytest.raises(RuntimeError, match="BACKUP_ROTATION_ENABLED is 1 but BACKUP_DIR is not set"):
        validate_rotation_safety()

    # Case 2: BACKUP_ROTATION_ENABLED=1 with invalid parameters fails closed
    backup_dir = tmp_path / "b_dir"
    backup_dir.mkdir()
    monkeypatch.setenv("BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("BACKUP_RETENTION_DAYS", "0")
    with pytest.raises(RuntimeError, match="retention_days must be > 0"):
        validate_rotation_safety()

    # Case 3: LOG_ROTATION_VERIFICATION_ENABLED=1 with max_days > 15 fails closed
    monkeypatch.setenv("BACKUP_ROTATION_ENABLED", "0")
    monkeypatch.setenv("LOG_ROTATION_VERIFICATION_ENABLED", "1")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setenv("LOG_DIR", str(log_dir))
    monkeypatch.setenv("LOG_RETENTION_MAX_DAYS", "30")
    with pytest.raises(RuntimeError, match="exceeds public policy commitment of 15 days"):
        validate_rotation_safety()

    # Case 4: Valid configuration passes cleanly
    monkeypatch.setenv("BACKUP_ROTATION_ENABLED", "1")
    monkeypatch.setenv("BACKUP_RETENTION_DAYS", "3")
    monkeypatch.setenv("BACKUP_KEEP_MIN", "2")
    monkeypatch.setenv("LOG_RETENTION_MAX_DAYS", "14")
    # All safety checks pass without error
    validate_rotation_safety()


def test_rotate_application_logs_positive_purges_expired(tmp_path):
    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)

    # 2 days old (kept)
    fresh_log = tmp_path / "app.log.1"
    fresh_log.write_text("fresh log entry")
    mtime_fresh = (now - timedelta(days=2)).timestamp()
    os.utime(fresh_log, (mtime_fresh, mtime_fresh))

    # 20 days old (purged)
    expired_log = tmp_path / "app.log.20"
    expired_log.write_text("old expired rotated log")
    mtime_old = (now - timedelta(days=20)).timestamp()
    os.utime(expired_log, (mtime_old, mtime_old))

    summary = log_rotation.rotate_application_logs(tmp_path, max_days=15, now=now, dry_run=False)
    assert summary["ok"] is True
    assert summary["scanned"] == 2
    assert summary["expired"] == 1
    assert summary["purged"] == 1
    assert not expired_log.exists()
    assert fresh_log.exists()


def test_rotate_application_logs_protects_active_logs(tmp_path):
    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)

    # Active base logs (app.log, access.log) are protected even if older than max_days
    app_log = tmp_path / "app.log"
    app_log.write_text("active app log")
    mtime_old = (now - timedelta(days=25)).timestamp()
    os.utime(app_log, (mtime_old, mtime_old))

    access_log = tmp_path / "access.log"
    access_log.write_text("active access log")
    os.utime(access_log, (mtime_old, mtime_old))

    # Log file written within last 24h is active
    recent_log = tmp_path / "other.log.1"
    recent_log.write_text("recently written log")
    mtime_recent = (now - timedelta(hours=3)).timestamp()
    os.utime(recent_log, (mtime_recent, mtime_recent))

    summary = log_rotation.rotate_application_logs(tmp_path, max_days=15, now=now, dry_run=False)
    assert summary["ok"] is True
    assert summary["purged"] == 0
    assert "app.log" in summary["protected_active"]
    assert "access.log" in summary["protected_active"]
    assert "other.log.1" in summary["protected_active"]
    assert app_log.exists()
    assert access_log.exists()
    assert recent_log.exists()


def test_rotate_application_logs_preserves_legal_hold(tmp_path):
    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)

    # File matching pattern *incident* (25 days old)
    incident_log = tmp_path / "incident_202610.log.old"
    incident_log.write_text("security incident logs")
    mtime_old = (now - timedelta(days=25)).timestamp()
    os.utime(incident_log, (mtime_old, mtime_old))

    # File with companion .hold file
    held_log = tmp_path / "audit_trail.log.1"
    held_log.write_text("audit records under legal hold")
    os.utime(held_log, (mtime_old, mtime_old))
    companion_hold = tmp_path / "audit_trail.log.1.hold"
    companion_hold.write_text("legal hold docket 2026-A")

    # Regular expired log (should be purged)
    regular_expired = tmp_path / "worker.log.25"
    regular_expired.write_text("ordinary rotated log")
    os.utime(regular_expired, (mtime_old, mtime_old))

    summary = log_rotation.rotate_application_logs(tmp_path, max_days=15, now=now, dry_run=False)
    assert summary["ok"] is True
    assert incident_log.name in summary["held"]
    assert held_log.name in summary["held"]
    assert incident_log.exists()
    assert held_log.exists()
    assert summary["purged"] == 1
    assert not regular_expired.exists()


def test_retention_worker_log_rotation_integration_and_lease_fencing(tmp_path, monkeypatch):
    import sqlite3
    import retention_worker

    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
    db_path = tmp_path / "orders.db"
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()
    log_dir = tmp_path / "app_logs"
    log_dir.mkdir()

    monkeypatch.setattr(main, "DB_PATH", db_path)
    main.init_db()

    # Expired log
    expired_log = log_dir / "server.log.20"
    expired_log.write_text("expired server log")
    mtime_old = (now - timedelta(days=20)).timestamp()
    os.utime(expired_log, (mtime_old, mtime_old))

    monkeypatch.setenv("LOG_ROTATION_ENABLED", "1")
    monkeypatch.setenv("LOG_DIR", str(log_dir))

    # Run cycle 1: lease acquired, rotation executed
    summary = retention_worker.run_retention_cycle(
        db_path,
        uploads_dir,
        log_dir=log_dir,
        now=now,
        instance_id="worker-primary",
    )
    assert summary["ok"] is True
    assert summary["log_rotation"]["purged"] == 1
    assert not expired_log.exists()

    # Telemetry verified
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT details_json FROM retention_telemetry WHERE instance_id='worker-primary'").fetchone()
        assert row is not None
        import json
        details = json.loads(row[0])
        assert details.get("logs_purged") == 1

    # Lease fencing test: peer worker cannot acquire lease concurrently
    peer_summary = retention_worker.run_retention_cycle(
        db_path,
        uploads_dir,
        log_dir=log_dir,
        now=now,
        instance_id="worker-peer",
    )
    assert peer_summary["ok"] is False
    assert peer_summary["reason"] == "lease_held_by_peer"


def test_rotate_application_logs_failure_injection_read_only(tmp_path, monkeypatch):
    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
    expired_log = tmp_path / "locked.log.20"
    expired_log.write_text("locked log")
    mtime_old = (now - timedelta(days=20)).timestamp()
    os.utime(expired_log, (mtime_old, mtime_old))

    # Simulate OS unlink failure (read-only or file lock)
    def fail_unlink(self):
        raise PermissionError("Access is denied: simulated locked file")

    monkeypatch.setattr(Path, "unlink", fail_unlink)

    summary = log_rotation.rotate_application_logs(tmp_path, max_days=15, now=now, dry_run=False)
    assert summary["ok"] is False
    assert summary["purged"] == 0
    assert len(summary["errors"]) == 1
    assert "Access is denied" in summary["errors"][0]

