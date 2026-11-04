"""Restore rehearsal: prove a real backup can be restored and safely resume.

This is the end-to-end evidence for S6-01. It uses the production code path
(``create_backup.generate_backup_atomic``) rather than a hand-made archive, then
restores into a fresh directory and checks the things that actually matter after a
disaster:

* the database identity and the full schema survive, including the paid-email
  outbox added by PR #26;
* every order, delivery state and outbox state survives row-for-row;
* restored uploads are byte-identical (SHA-256), and the project's own verifier
  accepts the restored pair;
* an ``uncertain``/``sending`` email can never be automatically resent;
* an unpaid order still cannot be delivered or cleaned;
* legitimate work can resume from the restored database;
* a failed backup attempt leaves the previous valid backup untouched.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
SCRIPTS_DIR = REPO_DIR / "scripts"
for _p in (str(BACKEND_DIR), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import backup_safety as safety  # noqa: E402
import create_backup  # noqa: E402
import delivery  # noqa: E402
import main  # noqa: E402
import paid_email_outbox  # noqa: E402
from backup_set_builder import build_database, extract_uploads, sha256_bytes  # noqa: E402
from verify_backup_integrity import APPLICATION_ID, verify_backup  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture(scope="module")
def rehearsal(tmp_path_factory):
    """Create the source environment, run a real backup, and restore it."""
    root = tmp_path_factory.mktemp("rehearsal")
    source = root / "source"
    info = build_database(source / "orders.db", source / "uploads", with_outbox=True)

    backups = root / "backups"
    backups.mkdir()
    target = backups / "20260115T120000Z"

    success, report = create_backup.generate_backup_atomic(
        db_path=source / "orders.db",
        uploads_dir=source / "uploads",
        output_dir=target,
        legacy_p0_policy="fail",
    )
    assert success, report.get("errors")

    # Restore into a completely separate location.
    restored = root / "restored"
    restored.mkdir()
    shutil.copy2(target / "orders.db", restored / "orders.db")
    restored_uploads = extract_uploads(target, restored)
    return {
        "root": root, "source": source, "info": info,
        "target": target, "report": report,
        "restored_db": restored / "orders.db",
        "restored_uploads": restored_uploads,
    }


def test_backup_published_atomically(rehearsal):
    report, target = rehearsal["report"], rehearsal["target"]
    assert report["published"] is True
    assert report["atomic"] is True
    assert report["output_dir"] == str(target)
    # The staging directory must not survive publication.
    assert not list(target.parent.glob(f"{safety.TMP_PREFIX}*"))


def test_backup_records_checksums_and_verification(rehearsal):
    target = rehearsal["target"]
    manifest = json.loads((target / "backup_manifest.json").read_text(encoding="utf-8"))
    assert manifest["recovery_verification_result"] == "PASS"
    assert len(manifest["orders_db_sha256"]) == 64
    assert len(manifest["uploads_tar_sha256"]) == 64

    ok, report = safety.verify_backup_set(
        target, require_outbox=True, max_age_hours=None, check_upload_archive=True)
    assert ok, report["errors"]
    assert report["application_id"] == APPLICATION_ID
    assert report["outbox_present"] is True


def test_restored_database_identity_and_schema(rehearsal):
    db = rehearsal["restored_db"]
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {"orders", "delivery_jobs", "paid_email_outbox"} <= tables


def test_restored_rows_match_the_source(rehearsal):
    source_db = rehearsal["source"] / "orders.db"
    restored_db = rehearsal["restored_db"]
    checks = [
        "SELECT id, payment_status, paid_at IS NOT NULL FROM orders ORDER BY id",
        "SELECT id, order_id, kind, state, attempts, cleanup_state, cleanup_phase "
        "FROM delivery_jobs ORDER BY id",
        "SELECT order_id, state, attempts FROM paid_email_outbox ORDER BY order_id",
    ]
    conn_s = sqlite3.connect(source_db)
    conn_r = sqlite3.connect(restored_db)
    try:
        for query in checks:
            assert conn_s.execute(query).fetchall() == conn_r.execute(query).fetchall(), query
    finally:
        conn_s.close()
        conn_r.close()


def test_restored_upload_bytes_match_sha256(rehearsal):
    source_uploads = rehearsal["source"] / "uploads"
    restored_uploads = rehearsal["restored_uploads"]

    source_hashes = {p.name: sha256_bytes(p.read_bytes())
                     for p in source_uploads.iterdir() if p.is_file()}
    restored_hashes = {p.name: sha256_bytes(p.read_bytes())
                       for p in restored_uploads.iterdir() if p.is_file()}
    assert source_hashes == restored_hashes, "restored uploads differ from the source"
    assert source_hashes, "the rehearsal produced no uploads"


def test_project_verifier_accepts_the_restored_pair(rehearsal):
    ok, report = verify_backup(rehearsal["restored_db"], rehearsal["restored_uploads"])
    assert ok is True, report.get("errors")
    assert report["p1_orders_count"] == 5
    assert report["p1_verified_files"] >= 1


# --------------------------------------------------- outbox evidence preservation
def test_uncertain_and_sending_emails_cannot_be_resent(rehearsal):
    """The whole point of the outbox: an ambiguous attempt is never auto-retried."""
    db = rehearsal["restored_db"]
    outbox = rehearsal["info"]["outbox"]
    order_ids = rehearsal["info"]["order_ids"]

    sending = order_ids[[i for i, s in OUTBOX_TARGET_SENDING().items()][0]]
    for index, order_id in order_ids.items():
        state = outbox.get(order_id)
        if state not in ("sending", "uncertain"):
            continue
        result, token = paid_email_outbox.claim(db, order_id)
        assert result == "blocked", f"{state} row was claimable for resend"
        assert token is None


def OUTBOX_TARGET_SENDING():
    from backup_set_builder import OUTBOX_TARGET
    return OUTBOX_TARGET


def test_sent_rows_are_reported_as_sent_not_resent(rehearsal):
    db = rehearsal["restored_db"]
    outbox = rehearsal["info"]["outbox"]
    order_ids = rehearsal["info"]["order_ids"]
    sent_orders = [oid for oid, state in outbox.items() if state == "sent"]
    assert sent_orders, "the rehearsal must contain a delivered paid email"
    for order_id in sent_orders:
        result, token = paid_email_outbox.claim(db, order_id)
        assert result == "sent"
        assert token is None


def test_pending_outbox_row_is_claimable(rehearsal):
    db = rehearsal["restored_db"]
    outbox = rehearsal["info"]["outbox"]
    pending = [oid for oid, state in outbox.items() if state == "pending"]
    assert pending, "the rehearsal must contain a pending outbox row"
    result, token = paid_email_outbox.claim(db, pending[0])
    assert result == "send"
    assert isinstance(token, str) and token


# ------------------------------------------------- unpaid orders stay undelivered
def test_unpaid_order_is_never_claimed_or_delivered(rehearsal):
    db = rehearsal["restored_db"]
    unpaid = rehearsal["info"]["unpaid"]
    assert unpaid

    def fresh_copy(tmp: Path) -> Path:
        copy = tmp / "orders.db"
        shutil.copy2(db, copy)
        return copy

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        working = fresh_copy(Path(tmp))
        claimed = []
        for _ in range(50):
            job = delivery.claim_due(working)
            if job is None:
                break
            claimed.append((job["order_id"], job["kind"]))
        assert claimed, "expected some legitimate work to be claimable"

        forbidden = [(oid, kind) for oid, kind in claimed
                     if oid in unpaid and kind in ("file", "info")]
        assert forbidden == [], f"unpaid delivery was claimed after restore: {forbidden}"


def test_deliver_job_refuses_an_unpaid_order_after_restore(rehearsal):
    db = rehearsal["restored_db"]
    unpaid_order = rehearsal["info"]["unpaid"][0]

    saved = main.DB_PATH
    main.DB_PATH = db
    try:
        with pytest.raises(RuntimeError, match="paid"):
            main.require_paid_order(unpaid_order)
    finally:
        main.DB_PATH = saved


def test_paid_pending_job_can_resume_after_restore(rehearsal):
    """Restored work must be claimable — a backup that cannot resume is useless."""
    db = rehearsal["restored_db"]
    paid_orders = set(rehearsal["info"]["paid"])

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        working = Path(tmp) / "orders.db"
        shutil.copy2(db, working)
        resumed = []
        for _ in range(50):
            job = delivery.claim_due(working)
            if job is None:
                break
            if job["kind"] in ("file", "info"):
                assert job["order_id"] in paid_orders
                resumed.append(job["order_id"])
        assert resumed, "no paid delivery work could be resumed from the restored database"


# --------------------------------------------------------------- failure injection
def test_failed_backup_preserves_the_previous_valid_set(rehearsal):
    """A failed attempt must not damage the backup it was meant to replace."""
    target = rehearsal["target"]
    before = {name: hashlib.sha256((target / name).read_bytes()).hexdigest()
              for name in safety.REQUIRED_FILES}

    success, report = create_backup.generate_backup_atomic(
        db_path=rehearsal["source"] / "orders.db",
        uploads_dir=rehearsal["root"] / "missing-uploads-directory",
        output_dir=target,
        legacy_p0_policy="fail",
    )
    assert success is False
    assert report["published"] is False
    assert any("not published" in error or "uploads" in error.lower()
               for error in report["errors"])

    after = {name: hashlib.sha256((target / name).read_bytes()).hexdigest()
             for name in safety.REQUIRED_FILES}
    assert after == before, "the previous valid backup was modified by a failed attempt"

    ok, verify = safety.verify_backup_set(target, require_outbox=True, max_age_hours=None)
    assert ok, verify["errors"]
    assert not list(target.parent.glob(f"{safety.TMP_PREFIX}*")), (
        "a failed attempt must not leave staging directories behind")


def test_failed_backup_does_not_create_the_destination(tmp_path):
    """First-ever backup failing must leave nothing that looks like a backup."""
    target = tmp_path / "backups" / "20260101T000000Z"
    success, report = create_backup.generate_backup_atomic(
        db_path=tmp_path / "absent.db",
        uploads_dir=tmp_path / "absent-uploads",
        output_dir=target,
    )
    assert success is False
    assert not target.exists()


def test_previous_set_is_retained_on_successful_replacement(rehearsal, tmp_path):
    """Replacing a set keeps the old one addressable as '.previous'."""
    import shutil as _shutil
    root = tmp_path / "rotate"
    root.mkdir()
    target = root / "set"

    first_ok, first = create_backup.generate_backup_atomic(
        db_path=rehearsal["source"] / "orders.db",
        uploads_dir=rehearsal["source"] / "uploads",
        output_dir=target,
    )
    assert first_ok, first.get("errors")
    original_db_sha = hashlib.sha256((target / "orders.db").read_bytes()).hexdigest()

    staging = safety.staging_dir_for(target)
    staging.mkdir()
    _shutil.copytree(target, staging, dirs_exist_ok=True)
    safety.atomic_publish(staging, target)

    previous = root / (target.name + safety.PREVIOUS_SUFFIX)
    assert previous.is_dir()
    assert hashlib.sha256((previous / "orders.db").read_bytes()).hexdigest() == original_db_sha
