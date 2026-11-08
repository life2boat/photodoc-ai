"""Tests for the retention prototype: remote purge, backup rotation, paid holds.

Everything runs against mocks and temporary directories; no network, no production tokens.
"""
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup_rotation  # noqa: E402
import paid_hold  # noqa: E402
import remote_purge  # noqa: E402

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
ROOT = "PhotoDoc_AI"


class FakeDisk:
    def __init__(self, script=None):
        self.calls = []
        self.script = list(script or [])

    def delete(self, path):
        self.calls.append(path)
        if self.script:
            return self.script.pop(0)
        return "deleted", ""


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    yield conn
    conn.close()


def job(path, delivered_hours_ago=None, order_id=1):
    data = {"order_id": order_id, "remote_path": path}
    data["delivered_at"] = (NOW - timedelta(hours=delivered_hours_ago)).isoformat() if delivered_hours_ago is not None else None
    return data


# ---------------------------------------------------------------- ownership guard
@pytest.mark.parametrize("path,owned", [
    ("PhotoDoc_AI/1_a.jpg", True),
    ("PhotoDoc_AI/sub/1_a.jpg", True),
    ("PhotoDoc_AI", False),
    ("PhotoDoc_AI/../etc/passwd", False),
    ("PhotoDoc_Backup/1_a.jpg", False),
    ("/PhotoDoc_AI/1_a.jpg", True),
    ("", False),
])
def test_ownership_guard(path, owned):
    assert remote_purge.is_owned_path(path, ROOT) is owned


# ---------------------------------------------------------------- planning
def test_plan_selects_only_expired_delivered_owned():
    jobs = [
        job("PhotoDoc_AI/1_old.jpg", 73),
        job("PhotoDoc_AI/1_new.jpg", 10),
        job("PhotoDoc_AI/1_undelivered.jpg", None),
        job("OtherRoot/1_alien.jpg", 100),
    ]
    planned = remote_purge.plan_candidates(jobs, now=NOW, root=ROOT, ttl_hours=72)
    assert [p["remote_path"] for p in planned] == ["PhotoDoc_AI/1_old.jpg"]


# ---------------------------------------------------------------- purge
def test_purge_deletes_and_audits(db):
    client = FakeDisk()
    jobs = [job("PhotoDoc_AI/1_old.jpg", 80)]
    result = remote_purge.purge(db, client, now=NOW, root=ROOT, jobs=jobs)

    assert result["deleted"] == 1
    assert client.calls == ["PhotoDoc_AI/1_old.jpg"]
    row = db.execute("SELECT state, deleted_at FROM remote_deletion_audit").fetchone()
    assert row[0] == "deleted" and row[1] is not None


def test_existing_intent_is_not_duplicated(db):
    client = FakeDisk()
    jobs = [job("PhotoDoc_AI/1_old.jpg", 80)]
    remote_purge.purge(db, client, now=NOW, root=ROOT, jobs=jobs)
    remote_purge.purge(db, client, now=NOW, root=ROOT, jobs=jobs)

    assert len(client.calls) == 1
    assert db.execute("SELECT COUNT(*) FROM remote_deletion_audit").fetchone()[0] == 1


def test_dry_run_plans_without_calling_or_writing(db):
    client = FakeDisk()
    result = remote_purge.purge(db, client, now=NOW, root=ROOT, jobs=[job("PhotoDoc_AI/1_old.jpg", 80)],
                                dry_run=True)
    assert result["planned"] == 1
    assert client.calls == []
    assert db.execute("SELECT COUNT(*) FROM remote_deletion_audit").fetchone()[0] == 0


def test_unowned_path_is_refused(db):
    client = FakeDisk()
    result = remote_purge.purge(db, client, now=NOW, root=ROOT,
                                jobs=[job("OtherRoot/1_alien.jpg", 100)])
    assert client.calls == []
    assert result["planned"] == 0


def test_error_retries_on_next_run(db):
    client = FakeDisk([("error", "temporary network failure")])
    jobs = [job("PhotoDoc_AI/1_old.jpg", 80)]

    first = remote_purge.purge(db, client, now=NOW, root=ROOT, jobs=jobs)
    assert first["deleted"] == 0
    row = db.execute("SELECT state, attempts, next_attempt_at FROM remote_deletion_audit").fetchone()
    assert row[0] == "intent" and row[1] == 1 and row[2] is not None

    second = remote_purge.purge(db, client, now=NOW + timedelta(minutes=10), root=ROOT, jobs=jobs)
    assert second["deleted"] == 1
    assert second["recovered"] == 1
    assert len(client.calls) == 2


def test_interrupted_deletion_is_recovered(db):
    # simulate a crash after the intent commit, before the client call
    db.executescript(remote_purge.SCHEMA)
    db.execute("INSERT INTO remote_deletion_audit (remote_path, order_id, state, attempts, created_at, updated_at)"
               " VALUES ('PhotoDoc_AI/1_crashed.jpg', 1, 'intent', 0, ?, ?)", (NOW.isoformat(), NOW.isoformat()))
    db.commit()

    client = FakeDisk()
    result = remote_purge.purge(db, client, now=NOW, root=ROOT, jobs=[])
    assert result["recovered"] == 1
    assert result["deleted"] == 1
    assert client.calls == ["PhotoDoc_AI/1_crashed.jpg"]


def test_repeated_failures_end_as_failed(db):
    client = FakeDisk([("error", "boom")] * 10)
    jobs = [job("PhotoDoc_AI/1_old.jpg", 80)]
    moment = NOW
    for _ in range(6):
        remote_purge.purge(db, client, now=moment, root=ROOT, jobs=jobs)
        moment += timedelta(hours=2)
    row = db.execute("SELECT state, attempts FROM remote_deletion_audit").fetchone()
    assert row[0] == "failed"
    assert row[1] >= remote_purge.DEFAULT_MAX_ATTEMPTS


# ---------------------------------------------------------------- backup rotation
def make_backup(path: Path, days_old: float, size: int = 10):
    path.mkdir(parents=True)
    (path / "SHA256SUMS").write_text("x" * size)
    stamp = (NOW - timedelta(days=days_old)).timestamp()
    import os
    os.utime(path, (stamp, stamp))
    (path / "SHA256SUMS").touch()
    os.utime(path / "SHA256SUMS", (stamp, stamp))


def test_rotation_removes_expired_and_keeps_newest(tmp_path):
    make_backup(tmp_path / "pre-switch-A", 10)
    make_backup(tmp_path / "pre-switch-B", 5)
    make_backup(tmp_path / "pre-switch-C", 4)
    make_backup(tmp_path / "staging-D", 0.5)

    result = backup_rotation.rotate(tmp_path, now=NOW, retention_days=3, keep_min=2)

    names = {p.name for p in tmp_path.iterdir() if p.is_dir()}
    assert names == {"pre-switch-C", "staging-D"}
    assert len(result["removed"]) == 2
    assert (tmp_path / "rotation.jsonl").exists()


def test_rotation_keeps_minimum_even_if_all_old(tmp_path):
    for index, name in enumerate(("pre-switch-A", "pre-switch-B", "pre-switch-C")):
        make_backup(tmp_path / name, 30 + index)

    backup_rotation.rotate(tmp_path, now=NOW, retention_days=1, keep_min=2)
    remaining = {p.name for p in tmp_path.iterdir() if p.is_dir()}
    assert len(remaining) == 2


def test_rotation_ignores_foreign_entries_and_symlinks(tmp_path):
    make_backup(tmp_path / "pre-switch-A", 10)
    make_backup(tmp_path / "pre-switch-B", 9)
    make_backup(tmp_path / "pre-switch-C", 8)
    (tmp_path / "notes.txt").write_text("keep me")
    target = tmp_path / "pre-switch-REAL"
    make_backup(target, 20)
    link = tmp_path / "pre-switch-LINK"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not permitted on this platform")

    backup_rotation.rotate(tmp_path, now=NOW, retention_days=1, keep_min=1)

    assert (tmp_path / "notes.txt").exists()
    assert link.is_symlink()
    assert target.exists()


def test_rotation_dry_run_changes_nothing(tmp_path):
    make_backup(tmp_path / "pre-switch-A", 30)
    make_backup(tmp_path / "pre-switch-B", 20)
    make_backup(tmp_path / "pre-switch-C", 10)

    result = backup_rotation.rotate(tmp_path, now=NOW, retention_days=1, keep_min=1, dry_run=True)

    assert len(result["removed"]) == 2
    assert len([p for p in tmp_path.iterdir() if p.is_dir()]) == 3
    assert not (tmp_path / "rotation.jsonl").exists()


def test_rotation_missing_directory(tmp_path):
    result = backup_rotation.rotate(tmp_path / "missing", now=NOW)
    assert result["ok"] is False


# ---------------------------------------------------------------- paid holds
def make_orders_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, payment_status TEXT)")
    conn.execute("CREATE TABLE delivery_jobs (id TEXT PRIMARY KEY, order_id INTEGER, kind TEXT,"
                 " local_path TEXT, state TEXT, created_at TEXT)")
    conn.commit()
    return conn


def test_paid_undelivered_is_escalated_and_never_deleted(tmp_path):
    conn = make_orders_db(tmp_path / "orders.db")
    conn.execute("INSERT INTO orders VALUES (1, 'paid')")
    conn.execute("INSERT INTO delivery_jobs VALUES ('j1', 1, 'file', '/app/uploads/1_a.jpg', 'failed', ?)",
                 ((NOW - timedelta(hours=200)).isoformat(),))
    conn.commit()

    result = paid_hold.flag_escalations(conn, now=NOW)

    assert result["escalated"] == 1
    assert result["newly_escalated"] == 1
    queue = paid_hold.operator_queue(conn)
    assert queue[0]["order_id"] == 1
    # the module must not touch delivery_jobs
    assert conn.execute("SELECT state FROM delivery_jobs").fetchone()[0] == "failed"


def test_unpaid_orders_are_not_held(tmp_path):
    conn = make_orders_db(tmp_path / "orders.db")
    conn.execute("INSERT INTO orders VALUES (2, 'unpaid')")
    conn.execute("INSERT INTO delivery_jobs VALUES ('j2', 2, 'file', '/app/uploads/2_b.jpg', 'pending', ?)",
                 ((NOW - timedelta(hours=200)).isoformat(),))
    conn.commit()

    result = paid_hold.flag_escalations(conn, now=NOW)
    assert result["watching"] == 0 and result["escalated"] == 0


def test_hold_within_window_is_watched(tmp_path):
    conn = make_orders_db(tmp_path / "orders.db")
    conn.execute("INSERT INTO orders VALUES (3, 'paid')")
    conn.execute("INSERT INTO delivery_jobs VALUES ('j3', 3, 'file', '/app/uploads/3_c.jpg', 'pending', ?)",
                 ((NOW - timedelta(hours=2)).isoformat(),))
    conn.commit()

    result = paid_hold.flag_escalations(conn, now=NOW)
    assert result["watching"] == 1 and result["escalated"] == 0
    assert paid_hold.operator_queue(conn) == []


def test_escalation_is_recorded_once(tmp_path):
    conn = make_orders_db(tmp_path / "orders.db")
    conn.execute("INSERT INTO orders VALUES (4, 'paid')")
    conn.execute("INSERT INTO delivery_jobs VALUES ('j4', 4, 'file', '/app/uploads/4_d.jpg', 'failed', ?)",
                 ((NOW - timedelta(hours=300)).isoformat(),))
    conn.commit()

    paid_hold.flag_escalations(conn, now=NOW)
    second = paid_hold.flag_escalations(conn, now=NOW + timedelta(hours=1))
    assert second["newly_escalated"] == 0
    assert conn.execute("SELECT COUNT(*) FROM paid_hold_audit").fetchone()[0] == 1


def test_dry_run_writes_nothing(tmp_path):
    conn = make_orders_db(tmp_path / "orders.db")
    conn.execute("INSERT INTO orders VALUES (5, 'paid')")
    conn.execute("INSERT INTO delivery_jobs VALUES ('j5', 5, 'file', '/app/uploads/5_e.jpg', 'failed', ?)",
                 ((NOW - timedelta(hours=300)).isoformat(),))
    conn.commit()

    paid_hold.flag_escalations(conn, now=NOW, dry_run=True)
    assert conn.execute("SELECT COUNT(*) FROM paid_hold_audit").fetchone()[0] == 0
