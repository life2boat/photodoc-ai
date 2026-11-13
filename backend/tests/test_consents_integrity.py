"""Regression tests for H-04 (ordering) and H-07 (read must not commit)."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import consents  # noqa: E402


@pytest.fixture(autouse=True)
def synthetic_approval(monkeypatch):
    for p in consents.MANDATORY_PURPOSES:
        monkeypatch.setitem(consents.DOCUMENTS[p], "approved", True)
        monkeypatch.setitem(consents.DOCUMENTS[p], "legal_status", "SYNTHETIC_TEST_APPROVED")


def test_h04_future_dated_grant_cannot_override_withdrawal():
    conn = sqlite3.connect(":memory:")
    try:
        consents.record_consent(conn, order_ref="o-1", purpose=consents.PURPOSE_SERVICE,
                                recorded_at="2099-01-01T00:00:00+00:00")
        consents.withdraw_consent(conn, order_ref="o-1", purpose=consents.PURPOSE_SERVICE,
                                  recorded_at="2026-01-01T00:00:00+00:00")
        assert consents.current_state(conn, "o-1")[consents.PURPOSE_SERVICE] == "withdrawn"
        assert consents.PURPOSE_SERVICE in consents.check_required(conn, "o-1")
    finally:
        conn.close()


def test_h04_ordering_is_insertion_order_regardless_of_timestamps():
    conn = sqlite3.connect(":memory:")
    try:
        consents.record_consent(conn, order_ref="o-2", purpose=consents.PURPOSE_SERVICE,
                                recorded_at="2030-01-01T00:00:00+00:00")
        consents.record_consent(conn, order_ref="o-2", purpose=consents.PURPOSE_SERVICE,
                                action="withdrawn", recorded_at="2020-01-01T00:00:00+00:00")
        consents.record_consent(conn, order_ref="o-2", purpose=consents.PURPOSE_SERVICE,
                                recorded_at="2021-01-01T00:00:00+00:00")
        assert consents.current_state(conn, "o-2")[consents.PURPOSE_SERVICE] == "granted"
    finally:
        conn.close()


def test_h07_read_does_not_commit_the_callers_transaction():
    conn = sqlite3.connect(":memory:")
    conn.isolation_level = None
    try:
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY)")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO orders VALUES (1)")

        # a read-only consent query must neither create schema nor commit the order
        assert consents.current_state(conn, "unknown") == {}
        assert consents.check_required(conn, "unknown")
        assert consents.evidence(conn, "unknown") == []

        conn.execute("ROLLBACK")
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0, \
            "consent read committed the caller's order transaction"
    finally:
        conn.close()
