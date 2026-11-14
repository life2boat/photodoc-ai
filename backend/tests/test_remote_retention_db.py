"""DB-integration tests for the remote retention planner (isolated SQLite)."""
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import remote_retention  # noqa: E402

NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
ROOT = "PhotoDoc_AI"


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "orders.db")
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, payment_status TEXT)")
    conn.execute(
        "CREATE TABLE delivery_jobs (id TEXT PRIMARY KEY, order_id INTEGER, kind TEXT, "
        "state TEXT, remote_path TEXT, delivered_at TEXT)"
    )
    conn.execute("INSERT INTO orders VALUES (1, 'paid')")
    conn.commit()
    yield conn
    conn.close()


def add(conn, path, hours_ago, state="delivered", kind="file"):
    delivered = (NOW - timedelta(hours=hours_ago)).isoformat() if hours_ago is not None else None
    conn.execute(
        "INSERT INTO delivery_jobs VALUES (?, 1, ?, ?, ?, ?)",
        (uuid.uuid4().hex, kind, state, path, delivered),
    )
    conn.commit()


def test_plan_from_db_selects_only_expired_delivered_owned(db):
    add(db, "PhotoDoc_AI/1_old.jpg", 73)
    add(db, "PhotoDoc_AI/1_new.jpg", 10)
    add(db, "PhotoDoc_AI/1_pending.jpg", 100, state="pending")
    add(db, "Other/1_alien.jpg", 100)

    planned = remote_retention.plan_from_db(db, now=NOW, root=ROOT)

    assert [p["remote_path"] for p in planned] == ["PhotoDoc_AI/1_old.jpg"]


def test_purge_with_db_candidates_deletes_and_audits(db):
    add(db, "PhotoDoc_AI/1_old.jpg", 80)
    planned = remote_retention.plan_from_db(db, now=NOW, root=ROOT)

    class Client:
        def __init__(self):
            self.calls = []
        def delete(self, path):
            self.calls.append(path)
            return "deleted", ""

    client = Client()
    result = remote_retention.purge(db, client, now=NOW, root=ROOT, jobs=planned)

    assert result["deleted"] == 1
    assert client.calls == ["PhotoDoc_AI/1_old.jpg"]
    row = db.execute("SELECT state FROM remote_deletion_audit").fetchone()
    assert row[0] == "deleted"


def test_plan_from_db_skips_rows_without_delivery_time(db):
    add(db, "PhotoDoc_AI/1_no_date.jpg", None)
    assert remote_retention.plan_from_db(db, now=NOW, root=ROOT) == []
