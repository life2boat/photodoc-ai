"""Deployment backup gate: it must refuse a cutover that has no usable rollback.

The gate is the only thing standing between "we have a backup" and "we can
actually go back". These tests pin the refusals, the distinct exit codes, and the
coverage rule that a valid-but-too-old backup is still unsafe.
"""

from __future__ import annotations

import json
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

import backup_gate  # noqa: E402
from backup_set_builder import build_database, make_backup_set  # noqa: E402

NOW = datetime.now(timezone.utc)


def run_gate(argv):
    return backup_gate.main(argv)


def test_gate_passes_for_a_current_verified_set(tmp_path):
    backup_dir, _ = make_backup_set(tmp_path)
    assert run_gate(["--backup-dir", str(backup_dir), "--require-outbox"]) == 0


def test_gate_passes_with_deep_archive_check(tmp_path):
    backup_dir, _ = make_backup_set(tmp_path)
    assert run_gate(["--backup-dir", str(backup_dir), "--require-outbox", "--deep"]) == 0


def test_gate_fails_when_no_backup_exists(tmp_path):
    assert run_gate(["--backup-dir", str(tmp_path / "absent"), "--require-outbox"]) == backup_gate.EXIT_MISSING


def test_gate_fails_when_the_manifest_is_not_a_pass(tmp_path):
    backup_dir, _ = make_backup_set(tmp_path, verification="FAIL")
    assert run_gate(["--backup-dir", str(backup_dir), "--require-outbox"]) == \
        backup_gate.EXIT_UNVERIFIED


def test_gate_allows_unverified_only_when_explicitly_permitted(tmp_path):
    backup_dir, _ = make_backup_set(tmp_path, verification="FAIL")
    assert run_gate(["--backup-dir", str(backup_dir), "--allow-unverified"]) == 0


def test_gate_fails_when_the_set_is_stale(tmp_path):
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    backup_dir, _ = make_backup_set(tmp_path, created=old)
    assert run_gate(["--backup-dir", str(backup_dir), "--max-age-hours", "24",
                     "--require-outbox"]) == backup_gate.EXIT_STALE


def test_gate_requires_the_outbox(tmp_path):
    backup_dir, _ = make_backup_set(tmp_path, with_outbox=False)
    assert run_gate(["--backup-dir", str(backup_dir), "--require-outbox"]) != 0
    # Without the requirement a pre-PR #26 set is still a usable rollback.
    assert run_gate(["--backup-dir", str(backup_dir)]) == 0


def test_latest_of_selects_the_newest_complete_set(tmp_path):
    make_backup_set(tmp_path, name="20260101T000000Z", created=NOW - timedelta(days=2))
    newest, _ = make_backup_set(tmp_path, name="20260102T000000Z", created=NOW - timedelta(hours=1))

    code = run_gate(["--latest-of", str(tmp_path), "--require-outbox"])
    assert code == 0
    selected = backup_gate.latest_backup_set(tmp_path, require_outbox=True)
    assert selected == newest


def test_latest_of_skips_incomplete_sets(tmp_path):
    make_backup_set(tmp_path, name="20260101T000000Z", created=NOW - timedelta(days=2))
    make_backup_set(tmp_path, name="20260102T000000Z", verification="FAIL", created=NOW)

    selected = backup_gate.latest_backup_set(tmp_path, require_outbox=True)
    assert selected is not None
    assert selected.name == "20260101T000000Z", "an unverified set must not be selected"


def test_latest_of_with_nothing_usable(tmp_path):
    assert backup_gate.latest_backup_set(tmp_path) is None
    assert run_gate(["--latest-of", str(tmp_path), "--require-outbox"]) == backup_gate.EXIT_MISSING


# ------------------------------------------------------- coverage of live data
def _live_database(tmp_path, newest_order: datetime) -> Path:
    db = tmp_path / "live" / "orders.db"
    uploads = tmp_path / "live" / "uploads"
    build_database(db, uploads, with_outbox=True)
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE orders SET created_at=?", (newest_order.isoformat(),))
        conn.commit()
    finally:
        conn.close()
    return db


def test_gate_rejects_a_backup_older_than_the_newest_order(tmp_path):
    """A perfectly valid backup taken before the latest order is still unsafe."""
    backup_dir, _ = make_backup_set(tmp_path / "backups", created=NOW - timedelta(hours=6))
    live_db = _live_database(tmp_path, NOW)

    code = run_gate(["--backup-dir", str(backup_dir), "--live-db", str(live_db),
                     "--require-outbox", "--max-age-hours", "48"])
    assert code == backup_gate.EXIT_STALE


def test_gate_accepts_a_backup_newer_than_the_newest_order(tmp_path):
    backup_dir, _ = make_backup_set(tmp_path / "backups", created=NOW + timedelta(minutes=1))
    live_db = _live_database(tmp_path, NOW)

    code = run_gate(["--backup-dir", str(backup_dir), "--live-db", str(live_db),
                     "--require-outbox", "--max-age-hours", "48"])
    assert code == 0


def test_gate_reports_live_coverage_in_json(tmp_path, capsys):
    backup_dir, _ = make_backup_set(tmp_path / "backups", created=NOW + timedelta(minutes=1))
    live_db = _live_database(tmp_path, NOW)

    run_gate(["--backup-dir", str(backup_dir), "--live-db", str(live_db),
              "--require-outbox", "--max-age-hours", "48", "--json"])
    report = json.loads(capsys.readouterr().out)
    coverage = report["checks"]["live_coverage"]
    assert coverage["live_orders"] == 5
    assert coverage["newest_order"] is not None
    assert report["ok"] is True


def test_gate_rejects_a_missing_live_database(tmp_path):
    backup_dir, _ = make_backup_set(tmp_path / "backups")
    code = run_gate(["--backup-dir", str(backup_dir),
                     "--live-db", str(tmp_path / "no-such.db"), "--require-outbox"])
    assert code == backup_gate.EXIT_STALE


# ---------------------------------------------------------------- quiescence
def test_gate_requires_quiescence_when_asked(tmp_path):
    backup_dir, _ = make_backup_set(tmp_path / "backups")
    live_db = _live_database(tmp_path, NOW - timedelta(hours=1))

    code = run_gate(["--backup-dir", str(backup_dir), "--db", str(live_db),
                     "--require-quiescent", "--worker-enabled", "yes", "--require-outbox"])
    assert code == backup_gate.EXIT_NOT_QUIESCENT


def test_gate_passes_quiescence_for_an_idle_database(tmp_path):
    backup_dir, _ = make_backup_set(tmp_path / "backups")
    live_db = _live_database(tmp_path, NOW - timedelta(hours=1))
    conn = sqlite3.connect(live_db)
    try:
        conn.execute("UPDATE delivery_jobs SET state='delivered' WHERE state='processing'")
        conn.commit()
    finally:
        conn.close()

    code = run_gate(["--backup-dir", str(backup_dir), "--db", str(live_db),
                     "--require-quiescent", "--worker-enabled", "no", "--require-outbox"])
    assert code == 0


def test_json_output_is_machine_readable_on_failure(tmp_path, capsys):
    code = run_gate(["--backup-dir", str(tmp_path / "absent"), "--require-outbox", "--json"])
    assert code == backup_gate.EXIT_MISSING
    payload = json.loads(capsys.readouterr().out)
    assert payload["gate"] == "backup"
    assert payload["ok"] is False
    assert payload["errors"]
