"""Deterministic two-connection payment/quarantine ordering on synthetic data."""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

import delivery
import retention

NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)


@pytest.fixture
def artifact(tmp_path):
    uploads = tmp_path / "uploads"
    quarantine = uploads / ".quarantine"
    quarantine.mkdir(parents=True)
    source = uploads / "1_source.jpg"
    claim = quarantine / "job1.owner.claim"
    claim.write_bytes(b"synthetic recoverable original")
    db = tmp_path / "orders.db"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("CREATE TABLE orders(id INTEGER PRIMARY KEY,payment_status TEXT)")
        conn.executescript(delivery.DELIVERY_SCHEMA + retention.RETENTION_SCHEMA)
        conn.execute("INSERT INTO orders VALUES(1,'unpaid')")
        conn.execute(
            "INSERT INTO delivery_jobs(id,order_id,kind,local_path,state,created_at,"
            "next_attempt_at,artifact_role) VALUES('job1',1,'file',?,'pending',?,?,'source')",
            (str(source), NOW.isoformat(), NOW.isoformat()))
        conn.execute(
            "INSERT INTO retention_audit(job_id,order_id,action,path,created_at) "
            "VALUES('job1',1,'deleted',?,?)",
            (str(source), (NOW - timedelta(days=2)).isoformat()))
    return db, uploads, source, claim


def update(db, sql, args=()):
    with closing(sqlite3.connect(db, timeout=1)) as conn, conn:
        conn.execute(sql, args)


@pytest.mark.parametrize("status", ["paid", "pending"])
def test_payment_committed_before_eligibility_preserves_original(artifact, status):
    db, uploads, source, claim = artifact
    update(db, "UPDATE orders SET payment_status=?", (status,))
    result = retention.purge_quarantines(db, uploads, now=NOW)
    assert result["restored"] == 1 and result["released"] == 0
    assert source.read_bytes() == b"synthetic recoverable original"
    assert not claim.exists()


def test_payment_writer_wins_while_purge_waits_for_eligibility_lock(artifact, monkeypatch):
    db, uploads, source, claim = artifact
    attempted = threading.Event()
    real_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(lambda sql: attempted.set() if sql == "BEGIN IMMEDIATE" else None)
        return conn

    with closing(real_connect(db)) as payer:
        payer.execute("BEGIN IMMEDIATE")
        payer.execute("UPDATE orders SET payment_status='paid'")
        monkeypatch.setattr(retention.sqlite3, "connect", traced_connect)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(retention.purge_quarantines, db, uploads, now=NOW)
            try:
                assert attempted.wait(5), "purge never attempted its eligibility lock"
            finally:
                payer.commit()  # committed while purge's BEGIN is waiting
            result = future.result(timeout=5)
    assert result["restored"] == 1 and not result["errors"]
    assert source.exists() and not claim.exists()


def test_payment_during_unlink_is_fenced_until_purge_completes(artifact, monkeypatch):
    db, uploads, source, claim = artifact
    real_delete = retention._delete_quarantine
    blocked = []

    def attempt_payment_then_unlink(path):
        # A distinct SQLite writer must fail immediately rather than commit in
        # the historical eligibility->unlink gap. No sleeps or scheduler races.
        with closing(sqlite3.connect(db, timeout=0)) as payer:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                payer.execute("UPDATE orders SET payment_status='paid'")
            blocked.append(True)
        assert path.exists()
        return real_delete(path)

    monkeypatch.setattr(retention, "_delete_quarantine", attempt_payment_then_unlink)
    result = retention.purge_quarantines(db, uploads, now=NOW)
    assert blocked == [True] and result["released"] == 1 and not result["errors"]
    assert not source.exists() and not claim.exists()
    # Linearization: payment after completed unpaid purge is a late payment;
    # the existing payment-gate suite requires SourceMissingAtPayment handling.
    update(db, "UPDATE orders SET payment_status='paid'")


@pytest.mark.parametrize("mutation", [
    "DELETE FROM orders", "DELETE FROM delivery_jobs",
    "UPDATE orders SET payment_status=NULL",
    "UPDATE orders SET payment_status='unknown'",
    "UPDATE delivery_jobs SET local_path='/outside/source.jpg'",
    "UPDATE delivery_jobs SET artifact_role='quarantine'",
    "UPDATE delivery_jobs SET artifact_role='result'",
    "UPDATE delivery_jobs SET state='processing'",
])
def test_unverifiable_or_changed_artifact_state_is_preserved(artifact, mutation):
    db, uploads, source, claim = artifact
    update(db, mutation)
    result = retention.purge_quarantines(db, uploads, now=NOW)
    assert result["released"] == 0 and claim.exists()


def test_unlink_failure_preserves_copy_and_allows_retry(artifact, monkeypatch):
    db, uploads, source, claim = artifact
    with monkeypatch.context() as patch:
        patch.setattr(retention, "_delete_quarantine", lambda path: False)
        result = retention.purge_quarantines(db, uploads, now=NOW)
    assert claim.exists() and result["errors"] and result["released"] == 0
    result = retention.purge_quarantines(db, uploads, now=NOW)
    assert result["released"] == 1 and not result["errors"] and not claim.exists()


def test_dry_run_and_grace_window_never_unlink(artifact):
    db, uploads, source, claim = artifact
    assert retention.purge_quarantines(db, uploads, now=NOW, dry_run=True)["released"] == 1
    assert claim.exists()
    assert retention.purge_quarantines(db, uploads, now=NOW, grace_hours=72)["released"] == 0
    assert claim.exists()


def test_failed_paid_restore_keeps_copy_and_releases_lock(artifact, monkeypatch):
    db, uploads, source, claim = artifact
    update(db, "UPDATE orders SET payment_status='paid'")
    with monkeypatch.context() as patch:
        patch.setattr(retention, "_restore", lambda *args: False)
        result = retention.purge_quarantines(db, uploads, now=NOW)
    assert claim.exists() and result["kept_for_recovery"] == 1
    assert retention.purge_quarantines(db, uploads, now=NOW)["restored"] == 1
    assert source.exists()


def test_symlink_claim_cannot_delete_outside_sentinel(artifact, tmp_path):
    db, uploads, source, claim = artifact
    sentinel = tmp_path / "outside-synthetic.txt"
    sentinel.write_bytes(b"synthetic outside sentinel")
    claim.unlink()
    try:
        claim.symlink_to(sentinel)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    result = retention.purge_quarantines(db, uploads, now=NOW)
    assert result["released"] == 0
    assert sentinel.read_bytes() == b"synthetic outside sentinel" and claim.is_symlink()


def test_symlink_quarantine_directory_is_not_followed(artifact, tmp_path):
    db, uploads, source, claim = artifact
    outside = tmp_path / "outside-quarantine"
    claim.parent.rename(outside)
    try:
        (uploads / ".quarantine").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation unavailable: {exc}")
    result = retention.purge_quarantines(db, uploads, now=NOW)
    assert result["released"] == 0
    assert (outside / claim.name).read_bytes() == b"synthetic recoverable original"
