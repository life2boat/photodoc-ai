"""Backup safety primitives: atomic publication, set verification, quiescence, retention.

These tests are about the properties that make a backup *usable*: that a failed
run cannot destroy the previous copy, that a set which looks complete is actually
restorable, that the outbox evidence survives, and that a stale set is rejected
before it is relied on.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
SCRIPTS_DIR = REPO_DIR / "scripts"
for _p in (str(BACKEND_DIR), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import backup_safety as safety  # noqa: E402
from backup_set_builder import make_backup_set, build_database, extract_uploads  # noqa: E402

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def good_set(tmp_path_factory):
    root = tmp_path_factory.mktemp("backup-good")
    backup_dir, info = make_backup_set(root, created=NOW)
    return backup_dir, info


# ------------------------------------------------------------------ atomic publish
def test_atomic_publish_creates_destination(tmp_path):
    staging = tmp_path / ".tmp-new"
    staging.mkdir()
    (staging / "payload").write_text("new", encoding="utf-8")
    final = tmp_path / "20260101T000000Z"

    published = safety.atomic_publish(staging, final)

    assert published == final
    assert (final / "payload").read_text(encoding="utf-8") == "new"
    assert not staging.exists()


def test_atomic_publish_preserves_previous_set(tmp_path):
    final = tmp_path / "20260101T000000Z"
    final.mkdir()
    (final / "payload").write_text("old", encoding="utf-8")

    staging = tmp_path / ".tmp-new"
    staging.mkdir()
    (staging / "payload").write_text("new", encoding="utf-8")

    safety.atomic_publish(staging, final)

    previous = tmp_path / (final.name + safety.PREVIOUS_SUFFIX)
    assert (final / "payload").read_text(encoding="utf-8") == "new"
    assert previous.is_dir()
    assert (previous / "payload").read_text(encoding="utf-8") == "old", (
        "the previous valid backup must remain readable after a publish")


def test_atomic_publish_restores_previous_when_publishing_fails(tmp_path, monkeypatch):
    final = tmp_path / "20260101T000000Z"
    final.mkdir()
    (final / "payload").write_text("old", encoding="utf-8")
    staging = tmp_path / ".tmp-new"
    staging.mkdir()
    (staging / "payload").write_text("new", encoding="utf-8")

    real_replace = os.replace

    def failing_replace(src, dst):
        if Path(src) == staging:
            raise OSError("simulated publish failure")
        return real_replace(src, dst)

    monkeypatch.setattr(safety.os, "replace", failing_replace)

    with pytest.raises(safety.BackupSafetyError):
        safety.atomic_publish(staging, final)

    assert final.is_dir(), "the destination must still exist after a failed publish"
    assert (final / "payload").read_text(encoding="utf-8") == "old", (
        "a failed publish must leave the previous valid backup in place")


def test_atomic_publish_refuses_without_replace(tmp_path):
    final = tmp_path / "existing"
    final.mkdir()
    staging = tmp_path / ".tmp-new"
    staging.mkdir()
    with pytest.raises(safety.BackupSafetyError):
        safety.atomic_publish(staging, final, replace=False)


def test_atomic_publish_rejects_missing_and_foreign_staging(tmp_path):
    final = tmp_path / "dest"
    with pytest.raises(safety.BackupSafetyError):
        safety.atomic_publish(tmp_path / "does-not-exist", final)

    outside = tmp_path.parent / "outside-staging"
    outside.mkdir(exist_ok=True)
    try:
        with pytest.raises(safety.BackupSafetyError):
            safety.atomic_publish(outside, tmp_path / "nested" / "dest")
    finally:
        outside.rmdir()


def test_staging_dir_is_hidden_and_sibling(tmp_path):
    final = tmp_path / "backup-set"
    staging = safety.staging_dir_for(final)
    assert staging.parent == final.parent
    assert staging.name.startswith(safety.TMP_PREFIX)
    assert staging != final


# --------------------------------------------------------------------- verification
def test_verifies_a_complete_backup_set(good_set):
    backup_dir, info = good_set
    ok, report = safety.verify_backup_set(backup_dir, require_outbox=True, now=NOW)
    assert ok, report["errors"]
    assert report["sqlite_integrity"] == "ok"
    assert report["application_id"] == safety.APPLICATION_ID
    assert report["outbox_present"] is True
    assert report["outbox_states"] == {"pending": 1, "sending": 1, "sent": 1, "uncertain": 1}
    assert report["orders_count"] == 5


def test_deep_verification_of_the_upload_archive(good_set):
    backup_dir, _ = good_set
    ok, report = safety.verify_backup_set(
        backup_dir, require_outbox=True, now=NOW, check_upload_archive=True)
    assert ok, report["errors"]
    assert report.get("upload_archive_verified") is True


@pytest.mark.parametrize("missing", list(safety.REQUIRED_FILES))
def test_rejects_a_set_missing_a_required_file(tmp_path, missing):
    backup_dir, _ = make_backup_set(tmp_path, created=NOW, omit=[missing])
    ok, report = safety.verify_backup_set(backup_dir, require_outbox=True, now=NOW)
    assert ok is False
    assert any(missing in error for error in report["errors"])


def test_rejects_a_checksum_mismatch(tmp_path):
    backup_dir, _ = make_backup_set(tmp_path, created=NOW, corrupt_checksum_for="orders.db")
    ok, report = safety.verify_backup_set(backup_dir, require_outbox=True, now=NOW)
    assert ok is False
    assert any("checksum mismatch" in error for error in report["errors"])


def test_rejects_a_corrupt_database(tmp_path):
    backup_dir, _ = make_backup_set(tmp_path, created=NOW, corrupt_db=True)
    ok, report = safety.verify_backup_set(backup_dir, require_outbox=False, now=NOW)
    assert ok is False
    assert report["sqlite_integrity"] != "ok" or any(
        "integrity" in error for error in report["errors"])


def test_rejects_an_unverified_backup(tmp_path):
    backup_dir, _ = make_backup_set(tmp_path, created=NOW, verification="FAIL")
    ok, report = safety.verify_backup_set(backup_dir, require_outbox=True, now=NOW)
    assert ok is False
    assert any("PASS recovery verification" in error for error in report["errors"])

    ok_override, _ = safety.verify_backup_set(
        backup_dir, require_outbox=True, now=NOW, require_verified=False)
    assert ok_override is True


def test_rejects_a_stale_backup(tmp_path):
    old = datetime.now(timezone.utc) - timedelta(hours=48)
    backup_dir, _ = make_backup_set(tmp_path, created=old)
    ok, report = safety.verify_backup_set(backup_dir, require_outbox=True, max_age_hours=24)
    assert ok is False
    assert any("too old" in error for error in report["errors"])


def test_rejects_a_backup_from_the_future(tmp_path):
    future = datetime.now(timezone.utc) + timedelta(hours=3)
    backup_dir, _ = make_backup_set(tmp_path, created=future)
    ok, report = safety.verify_backup_set(backup_dir, require_outbox=False)
    assert ok is False
    assert any("future" in error for error in report["errors"])


def test_requires_the_outbox_when_asked(tmp_path):
    backup_dir, _ = make_backup_set(tmp_path, with_outbox=False, created=NOW)
    ok, report = safety.verify_backup_set(backup_dir, require_outbox=False, now=NOW)
    assert ok is True, report["errors"]

    ok_required, required_report = safety.verify_backup_set(
        backup_dir, require_outbox=True, now=NOW)
    assert ok_required is False
    assert any("outbox" in error for error in required_report["errors"])


def test_rejects_sent_row_without_acceptance_evidence(good_set, tmp_path):
    """'sent' must mean the provider accepted it — otherwise it is a guess."""
    backup_dir, _ = good_set
    staging = tmp_path / "mutated"
    staging.mkdir()
    for name in safety.REQUIRED_FILES:
        (staging / name).write_bytes((backup_dir / name).read_bytes())

    conn = sqlite3.connect(staging / "orders.db")
    try:
        conn.execute("UPDATE paid_email_outbox SET accepted_at=NULL WHERE state='sent'")
        conn.commit()
    finally:
        conn.close()
    from backup_safety import sha256_file
    from backup_set_builder import write_manifests
    write_manifests(staging, created=NOW, orders_count=5)

    ok, report = safety.verify_backup_set(staging, require_outbox=True, now=NOW)
    assert ok is False
    assert any("without accepted_at" in error for error in report["errors"])


def test_rejects_orphaned_outbox_rows(good_set, tmp_path):
    backup_dir, _ = good_set
    staging = tmp_path / "orphaned"
    staging.mkdir()
    for name in safety.REQUIRED_FILES:
        (staging / name).write_bytes((backup_dir / name).read_bytes())

    conn = sqlite3.connect(staging / "orders.db")
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO paid_email_outbox (order_id, state, attempts, updated_at) "
            "VALUES (999, 'pending', 0, '2026-01-15T12:00:00+00:00')")
        conn.commit()
    finally:
        conn.close()
    from backup_set_builder import write_manifests
    write_manifests(staging, created=NOW, orders_count=5)

    ok, report = safety.verify_backup_set(staging, require_outbox=True, now=NOW)
    assert ok is False
    assert any("missing order" in error for error in report["errors"])


def test_rejects_manifest_order_count_disagreement(tmp_path):
    backup_dir, _ = make_backup_set(tmp_path, created=NOW)
    manifest = json.loads((backup_dir / "backup_manifest.json").read_text(encoding="utf-8"))
    manifest["orders_count"] = 99
    (backup_dir / "backup_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    ok, report = safety.verify_backup_set(backup_dir, require_outbox=True, now=NOW)
    assert ok is False
    assert any("orders count disagrees" in error for error in report["errors"])


def test_rejects_an_unfinished_staging_directory(tmp_path):
    staging = tmp_path / f"{safety.TMP_PREFIX}backup"
    staging.mkdir()
    ok, report = safety.verify_backup_set(staging, now=NOW)
    assert ok is False
    assert any("unfinished staging" in error for error in report["errors"])


def test_rejects_a_missing_directory(tmp_path):
    ok, report = safety.verify_backup_set(tmp_path / "nope", now=NOW)
    assert ok is False
    assert report["sqlite_integrity"] == "unknown"


# ----------------------------------------------------------------------- quiescence
def test_detects_an_in_flight_job(tmp_path):
    db = tmp_path / "orders.db"
    uploads = tmp_path / "uploads"
    build_database(db, uploads, with_outbox=True)
    quiet, report = safety.check_quiescence(db)
    assert quiet is False
    assert report["in_flight_jobs"] == 1
    assert any("live lease" in reason for reason in report["reasons"])


def test_quiescent_once_the_lease_expires(tmp_path):
    db = tmp_path / "orders.db"
    uploads = tmp_path / "uploads"
    build_database(db, uploads, with_outbox=True)
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE delivery_jobs SET lease_until=? WHERE state='processing'",
                     ("2000-01-01T00:00:00+00:00",))
        conn.commit()
    finally:
        conn.close()

    quiet, report = safety.check_quiescence(db)
    assert quiet is True, report["reasons"]
    assert report["processing_rows"] == 1
    assert report["in_flight_jobs"] == 0


def test_worker_disabled_does_not_imply_quiescence(tmp_path):
    db = tmp_path / "orders.db"
    build_database(db, tmp_path / "uploads", with_outbox=True)
    quiet, report = safety.check_quiescence(db, worker_enabled=True)
    assert quiet is False
    assert any("worker is enabled" in reason for reason in report["reasons"])


def test_detects_in_flight_cleanup(tmp_path):
    db = tmp_path / "orders.db"
    build_database(db, tmp_path / "uploads", with_outbox=True)
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE delivery_jobs SET lease_until=NULL WHERE state='processing'")
        conn.execute("UPDATE delivery_jobs SET cleanup_phase='deleting' WHERE id=(SELECT id FROM delivery_jobs LIMIT 1)")
        conn.commit()
    finally:
        conn.close()

    quiet, report = safety.check_quiescence(db)
    assert quiet is False
    assert report["cleanup_in_flight"] == 1


# ------------------------------------------------------------------------ retention
def test_retention_always_keeps_the_newest():
    entries = [(Path(f"/b/{i}"), NOW - timedelta(days=i)) for i in range(10)]
    keep, prune = safety.plan_retention(entries, keep_last=1, keep_daily=0, keep_weekly=0, now=NOW)
    assert Path("/b/0") in keep
    assert Path("/b/0") not in prune


def test_retention_honours_keep_last():
    entries = [(Path(f"/b/{i}"), NOW - timedelta(days=i)) for i in range(10)]
    keep, prune = safety.plan_retention(entries, keep_last=3, keep_daily=0, keep_weekly=0, now=NOW)
    assert len(keep) == 3
    assert set(keep) == {Path("/b/0"), Path("/b/1"), Path("/b/2")}
    assert len(prune) == 7


def test_retention_keeps_one_per_day_within_the_window():
    entries = [
        (Path("/b/day0-a"), NOW),
        (Path("/b/day0-b"), NOW - timedelta(hours=2)),
        (Path("/b/day1"), NOW - timedelta(days=1)),
    ]
    keep, prune = safety.plan_retention(entries, keep_last=1, keep_daily=3, keep_weekly=0, now=NOW)
    assert Path("/b/day0-a") in keep
    assert Path("/b/day1") in keep
    assert Path("/b/day0-b") in prune


def test_retention_keeps_one_per_week_behind_the_daily_window():
    entries = [
        (Path("/b/recent"), NOW),
        (Path("/b/week-1"), NOW - timedelta(days=10)),
        (Path("/b/week-2"), NOW - timedelta(days=17)),
    ]
    keep, _ = safety.plan_retention(entries, keep_last=1, keep_daily=0, keep_weekly=8, now=NOW)
    assert Path("/b/week-1") in keep
    assert Path("/b/week-2") in keep


def test_retention_with_no_entries():
    keep, prune = safety.plan_retention([], now=NOW)
    assert keep == [] and prune == []
