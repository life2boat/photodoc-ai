"""Yandex transport and SMTP retry tests with synthetic responses only."""
import hashlib
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
import main  # noqa: E402
from delivery import enqueue, process_due, utc_now  # noqa: E402


class Response:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self.data = data or {}

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"synthetic HTTP {self.status_code}")


def test_yandex_upload_checks_put_and_metadata_without_overwrite(tmp_path, monkeypatch):
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"synthetic-photo")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setenv("YANDEX_DISK_TOKEN", "synthetic-oauth")
    state = {"uploaded": False, "puts": []}

    def fake_get(url, **kwargs):
        if url.endswith("/upload"):
            assert kwargs["params"]["overwrite"] == "false"
            return Response(data={"href": "https://upload.example.invalid/file"})
        if not state["uploaded"]:
            return Response(status=404)
        return Response(data={"type": "file", "sha256": digest, "size": source.stat().st_size})

    def fake_put(url, **kwargs):
        state["puts"].append(url)
        if url.startswith("https://upload."):
            assert b"".join(kwargs["content"]) == source.read_bytes()
            state["uploaded"] = True
        return Response(status=201)

    with patch.object(main.httpx, "get", side_effect=fake_get), patch.object(main.httpx, "put", side_effect=fake_put):
        assert main.upload_to_yandex_disk(str(source), "PhotoDoc_Orders/Заказ_1_Test/10x15_Матовая_Без_полей", "unique.jpg")
        put_count = len(state["puts"])
        assert main.upload_to_yandex_disk(str(source), "PhotoDoc_Orders/Заказ_1_Test/10x15_Матовая_Без_полей", "unique.jpg")
        assert len([url for url in state["puts"][put_count:] if url.startswith("https://upload.")]) == 0


def test_yandex_timeout_and_wrong_remote_file_preserve_local_source(tmp_path, monkeypatch):
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"synthetic-photo")
    monkeypatch.setenv("YANDEX_DISK_TOKEN", "synthetic-oauth")
    with patch.object(main.httpx, "put", side_effect=TimeoutError("synthetic timeout")):
        with pytest.raises(TimeoutError):
            main.upload_to_yandex_disk(str(source), "PhotoDoc_Orders/Заказ_1_Test/settings", "unique.jpg")
    assert source.is_file()
    with patch.object(main.httpx, "put", return_value=Response(status=201)), \
         patch.object(main.httpx, "get", return_value=Response(data={"type": "file", "sha256": "wrong", "size": 15})):
        with pytest.raises(RuntimeError, match="does not match"):
            main.upload_to_yandex_disk(str(source), "PhotoDoc_Orders/Заказ_1_Test/settings", "unique.jpg")
    assert source.is_file()


def test_persisted_paid_email_retries_after_smtp_error(tmp_path, monkeypatch):
    db = tmp_path / "orders.db"
    monkeypatch.setattr(main, "DB_PATH", db)
    main.init_db()
    with sqlite3.connect(db) as conn:
        order_id = conn.execute(
            "INSERT INTO orders (name, phone, payment_status, payment_amount, paid_at) "
            "VALUES ('Test', '7999', 'paid', '20.00', '2026-01-01')"
        ).lastrowid
        enqueue(conn, order_id=order_id, kind="paid_email")
    for key, value in {"SMTP_SERVER": "smtp.example.invalid", "SMTP_USER": "test@example.invalid",
                       "SMTP_PASSWORD": "synthetic", "EMAIL_TO": "owner@example.invalid"}.items():
        monkeypatch.setenv(key, value)
    with patch.object(main.smtplib, "SMTP_SSL", side_effect=TimeoutError("synthetic SMTP timeout")):
        process_due(db, main.deliver_job)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT state FROM delivery_jobs WHERE kind='paid_email'").fetchone()[0] == "failed"
        assert conn.execute("SELECT payment_email_sent_at FROM orders WHERE id=?", (order_id,)).fetchone()[0] is None
        conn.execute("UPDATE delivery_jobs SET next_attempt_at=? WHERE kind='paid_email'", (utc_now(),))
    with patch.object(main.smtplib, "SMTP_SSL") as smtp:
        process_due(db, main.deliver_job)
        process_due(db, main.deliver_job)
        assert smtp.return_value.__enter__.return_value.send_message.call_count == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT state FROM delivery_jobs WHERE kind='paid_email'").fetchone()[0] == "delivered"
        assert conn.execute("SELECT payment_email_sent_at FROM orders WHERE id=?", (order_id,)).fetchone()[0]
