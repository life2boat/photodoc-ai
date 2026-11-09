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
