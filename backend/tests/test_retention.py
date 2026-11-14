"""Retention purge: 72-hour deletion of customer files with safety guards."""
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import delivery  # noqa: E402
import retention  # noqa: E402

NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "orders.db"
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, payment_status TEXT NOT NULL DEFAULT 'unpaid')"
    )
    conn.executescript(delivery.DELIVERY_SCHEMA)
    conn.commit()
    conn.close()
    return db, uploads


def add_order(db: Path, order_id: int, status: str = "unpaid") -> None:
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT OR REPLACE INTO orders(id, payment_status) VALUES (?, ?)", (order_id, status))


def add_job(db: Path, uploads: Path, *, order_id: int, age_hours: float, state: str = "pending",
            name: str | None = None, payload: bytes = b"customer photo bytes") -> tuple[str, Path]:
    job_id = uuid.uuid4().hex
    filename = name or f"{order_id}_{uuid.uuid4().hex}.jpg"
    path = uploads / filename
    path.write_bytes(payload)
    created = (NOW - timedelta(hours=age_hours)).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO delivery_jobs (id, order_id, kind, local_path, original_name, remote_path,"
            " content_sha256, content_size, state, attempts, next_attempt_at, created_at, cleanup_state, cleanup_phase)"
            " VALUES (?, ?, 'file', ?, ?, ?, ?, ?, ?, 0, ?, ?, 'pending', 'pending')",
            (job_id, order_id, str(path), filename, f"PhotoDoc/{filename}", "sha", len(payload),
             state, created, created),
        )
    return job_id, path


def audit_actions(db: Path) -> list[str]:
    with sqlite3.connect(db) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='retention_audit'"
        ).fetchone()
        if not exists:
            return []
        return [row[0] for row in conn.execute("SELECT action FROM retention_audit ORDER BY id")]


def test_unpaid_expired_file_is_deleted(store):
    db, uploads = store
    add_order(db, 1, "unpaid")
    _, path = add_job(db, uploads, order_id=1, age_hours=73)

    result = retention.purge_expired(db, uploads, now=NOW)

    assert result["deleted"] == 1
    assert not path.exists()
    assert result["bytes_freed"] == len(b"customer photo bytes")
    assert audit_actions(db) == ["deleting", "deleted"]


def test_unpaid_file_inside_window_is_kept(store):
    db, uploads = store
    add_order(db, 1, "unpaid")
    _, path = add_job(db, uploads, order_id=1, age_hours=71)

    result = retention.purge_expired(db, uploads, now=NOW)

    assert result["deleted"] == 0
    assert path.exists()


def test_boundary_is_exclusive_at_cutoff(store):
    db, uploads = store
    add_order(db, 1, "unpaid")
    _, path = add_job(db, uploads, order_id=1, age_hours=72.5)

    retention.purge_expired(db, uploads, now=NOW)
    assert not path.exists()


def test_paid_undelivered_file_is_held_not_deleted(store):
    db, uploads = store
    add_order(db, 2, "paid")
    _, path = add_job(db, uploads, order_id=2, age_hours=100, state="failed")

    result = retention.purge_expired(db, uploads, now=NOW)

    assert result["held_paid"] == 1
    assert result["deleted"] == 0
    assert path.exists()
    assert audit_actions(db) == ["held_paid"]


def test_delivered_jobs_are_not_handled_here(store):
    db, uploads = store
    add_order(db, 3, "paid")
    _, path = add_job(db, uploads, order_id=3, age_hours=100, state="delivered")

    result = retention.purge_expired(db, uploads, now=NOW)

    assert result["scanned"] == 0
    assert path.exists()


def test_missing_file_is_skipped_without_error(store):
    db, uploads = store
    add_order(db, 4, "unpaid")
    _, path = add_job(db, uploads, order_id=4, age_hours=90)
    path.unlink()

    result = retention.purge_expired(db, uploads, now=NOW)

    assert result["skipped_missing"] == 1
    assert result["deleted"] == 0
    assert result["errors"] == []


def test_path_outside_uploads_is_refused(store, tmp_path):
    db, uploads = store
    add_order(db, 5, "unpaid")
    outside = tmp_path / "5_outside.jpg"
    outside.write_bytes(b"must survive")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO delivery_jobs (id, order_id, kind, local_path, state, attempts, next_attempt_at,"
            " created_at, cleanup_state, cleanup_phase) VALUES (?, 5, 'file', ?, 'pending', 0, ?, ?, 'pending', 'pending')",
            (uuid.uuid4().hex, str(outside), (NOW - timedelta(hours=200)).isoformat(),
             (NOW - timedelta(hours=200)).isoformat()),
        )

    result = retention.purge_expired(db, uploads, now=NOW)

    assert result["skipped_unsafe"] == 1
    assert outside.exists()


def test_filename_without_order_prefix_is_refused(store):
    db, uploads = store
    add_order(db, 6, "unpaid")
    _, path = add_job(db, uploads, order_id=6, age_hours=80, name="no_order_prefix.jpg")

    result = retention.purge_expired(db, uploads, now=NOW)

    assert result["skipped_mismatch"] == 1
    assert path.exists()


def test_second_run_is_idempotent(store):
    db, uploads = store
    add_order(db, 7, "unpaid")
    _, path = add_job(db, uploads, order_id=7, age_hours=80)

    first = retention.purge_expired(db, uploads, now=NOW)
    second = retention.purge_expired(db, uploads, now=NOW)

    assert first["deleted"] == 1
    assert second["deleted"] == 0
    assert not path.exists()
    assert audit_actions(db) == ["deleting", "deleted"]


def test_dry_run_reports_without_deleting(store):
    db, uploads = store
    add_order(db, 8, "unpaid")
    _, path = add_job(db, uploads, order_id=8, age_hours=80)

    result = retention.purge_expired(db, uploads, now=NOW, dry_run=True)

    assert result["deleted"] == 1
    assert path.exists()
    assert audit_actions(db) == []


def test_orphan_scan_removes_unreferenced_old_file(store):
    db, uploads = store
    add_order(db, 9, "unpaid")
    add_job(db, uploads, order_id=9, age_hours=1)
    orphan = uploads / "9_orphan.part"
    orphan.write_bytes(b"orphan")
    old = (NOW - timedelta(hours=100)).timestamp()
    import os
    os.utime(orphan, (old, old))

    result = retention.purge_expired(db, uploads, now=NOW, orphan_scan=True)

    assert result["orphans_deleted"] == 1
    assert not orphan.exists()


def test_orphan_scan_skips_acceptance_fixtures(store):
    db, uploads = store
    fixture = uploads / "acceptance_20261104_x" / "success" / "1_success.bin"
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(b"fixture")
    old = (NOW - timedelta(hours=500)).timestamp()
    import os
    os.utime(fixture, (old, old))

    result = retention.purge_expired(db, uploads, now=NOW, orphan_scan=True)

    assert result["orphans_deleted"] == 0
    assert fixture.exists()


def test_cli_dry_run_exit_code(store, monkeypatch, capsys):
    db, uploads = store
    add_order(db, 10, "unpaid")
    add_job(db, uploads, order_id=10, age_hours=80)
    monkeypatch.setattr(sys, "argv", ["retention_purge.py", "--db", str(db), "--uploads", str(uploads),
                                      "--dry-run", "--hours", "72"])

    assert retention.main() == 0
    output = capsys.readouterr().out
    assert '"dry_run": true' in output
