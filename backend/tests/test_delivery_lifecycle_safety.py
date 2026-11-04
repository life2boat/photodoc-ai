"""Order/payment/outbox integration with synthetic HTTP and real process crashes."""
import hashlib
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
import main
from delivery import cleanup_orphaned_temporary_files, process_due, process_due_cleanups, utc_now

PHOTO = b"\xff\xd8\xff" + b"synthetic photo" * 6000


class Disk:
    """A storage boundary, not a mock of queue/cleanup/payment implementation."""
    def __init__(self):
        self.objects = {}
        self.hrefs = {}
        self.uploads = []
        self.failure = None

    def response(self, url, status=200, data=None):
        return httpx.Response(status, json=data or {}, request=httpx.Request("GET", url))

    def get(self, url, **kwargs):
        if self.failure == "timeout":
            raise httpx.ReadTimeout("synthetic-sensitive-error", request=httpx.Request("GET", url))
        path = kwargs["params"]["path"]
        if url.endswith("/upload"):
            assert kwargs["params"]["overwrite"] == "false"
            href = f"https://upload.example.invalid/{len(self.hrefs)}?signature=synthetic-signed-secret"
            self.hrefs[href] = path
            return self.response(url, data={"href": href})
        if path not in self.objects:
            return self.response(url, 404)
        content = self.objects[path]
        return self.response(url, data={"type": "file", "size": len(content),
                                       "sha256": hashlib.sha256(content).hexdigest()})

    def put(self, url, **kwargs):
        if url not in self.hrefs:
            return self.response(url, 503 if self.failure == "unavailable" else 201)
        chunks = iter(kwargs["content"])
        if self.failure == "partial":
            assert next(chunks)
            raise httpx.WriteError("synthetic-sensitive-error", request=httpx.Request("PUT", url))
        content = b"".join(chunks)
        path = self.hrefs[url]
        self.objects[path] = content
        self.uploads.append(path)
        return self.response(url, 201)


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    db, uploads = tmp_path / "orders.db", tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(main, "DB_PATH", db)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads)
    for key, value in {"DELIVERY_WORKER_ENABLED": "0", "YANDEX_DISK_TOKEN": "synthetic-oauth-secret",
                       "ROBOKASSA_MERCHANT_LOGIN": "synthetic-merchant", "ROBOKASSA_PASSWORD1": "synthetic-p1",
                       "ROBOKASSA_PASSWORD2": "synthetic-p2", "ROBOKASSA_IS_TEST": "1",
                       "PHOTODOC_STAGING": "0", "PHOTODOC_ALLOW_FRESH_DB": "0",
                       "YANDEX_DISK_ROOT_DIR": "PhotoDoc_Orders"}.items():
        monkeypatch.setenv(key, value)
    main.init_db()
    disk = Disk()
    monkeypatch.setattr(main.httpx, "get", disk.get)
    monkeypatch.setattr(main.httpx, "put", disk.put)
    monkeypatch.setattr(main, "send_order_email", lambda *_: True)
    monkeypatch.setattr(main, "send_paid_order_email", lambda *_: True)
    client = TestClient(main.app)
    response = client.post("/api/order", data={"name": "Synthetic", "phone": "79990000000",
                           "format": "10x15", "paper": "Матовая", "crop": "Без полей"},
                           files={"files": ("photo.jpg", PHOTO, "image/jpeg")})
    assert response.status_code == 200, response.text
    # Delivery/recovery scenarios start after a confirmed payment. H1 unpaid
    # coverage lives in test_payment_delivery_reliability.py.
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE orders SET payment_status='paid',paid_at='2026-01-01' WHERE id=?", (response.json()["order_id"],))
    yield client, db, uploads, disk, response.json()
    client.close()


def jobs(db):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM delivery_jobs ORDER BY created_at, id")]


def retry(db):
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE delivery_jobs SET next_attempt_at=? WHERE state='failed'", (utc_now(),))


def test_verified_delivery_cleans_only_after_remote_bytes_match(lifecycle):
    _, db, uploads, disk, _ = lifecycle
    original = {j["remote_path"]: Path(j["local_path"]).read_bytes()
                for j in jobs(db) if j["kind"] in ("file", "info")}
    process_due(db, main.deliver_job, uploads_dir=uploads)
    assert disk.objects == original
    assert list(uploads.iterdir()) == []
    assert all(j["state"] == "delivered" for j in jobs(db))
    assert all(j["cleanup_state"] == "completed" and j["local_deleted_at"]
               for j in jobs(db) if j["kind"] in ("file", "info"))
    process_due(db, main.deliver_job, uploads_dir=uploads)
    assert len(disk.uploads) == 2


@pytest.mark.parametrize("failure", ["unavailable", "timeout", "partial"])
def test_delivery_failure_retains_bytes_and_retries_without_new_order(lifecycle, failure, caplog):
    _, db, uploads, disk, order = lifecycle
    originals = {j["local_path"]: Path(j["local_path"]).read_bytes()
                 for j in jobs(db) if j["local_path"]}
    disk.failure = failure
    process_due(db, main.deliver_job, uploads_dir=uploads)
    for path, content in originals.items():
        assert Path(path).read_bytes() == content
    assert all(j["state"] == "failed" and j["local_deleted_at"] is None
               for j in jobs(db) if j["local_path"])
    assert "synthetic-sensitive-error" not in caplog.text
    assert "synthetic-signed-secret" not in caplog.text
    assert "synthetic-oauth-secret" not in caplog.text
    retry(db)
    disk.failure = None
    process_due(db, main.deliver_job, uploads_dir=uploads)
    assert all(j["state"] == "delivered" for j in jobs(db))
    assert list(uploads.iterdir()) == []
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT id, payment_status, order_amount FROM orders").fetchall() == [
                (order["order_id"], "paid", "20.00")]


def run_child(code, *args):
    env = {**os.environ, "PYTHONPATH": str(BACKEND)}
    return subprocess.run([sys.executable, "-c", code, *map(str, args)], env=env,
                          capture_output=True, timeout=20)


def test_actual_process_crash_after_remote_commit_reconciles_without_reupload(lifecycle, tmp_path):
    _, db, uploads, disk, _ = lifecycle
    file_job = next(j for j in jobs(db) if j["kind"] == "file")
    receipt = tmp_path / "synthetic-remote-object"
    child = run_child("""
import os, sys
from pathlib import Path
from delivery import process_due
def send(job):
    with open(sys.argv[2], 'wb') as out:
        out.write(Path(job['local_path']).read_bytes()); out.flush(); os.fsync(out.fileno())
    os._exit(73)  # remote accepted bytes, SQLite finish has not run
process_due(Path(sys.argv[1]), send, limit=1, uploads_dir=Path(sys.argv[3]))
""", db, receipt, uploads)
    assert child.returncode == 73, child.stderr.decode(errors="replace")
    assert receipt.read_bytes() == PHOTO
    assert Path(file_job["local_path"]).read_bytes() == PHOTO
    assert jobs(db)[0]["state"] == "processing"
    disk.objects[file_job["remote_path"]] = receipt.read_bytes()
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE delivery_jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?",
                     (file_job["id"],))
    process_due(db, main.deliver_job, uploads_dir=uploads)
    assert file_job["remote_path"] not in disk.uploads
    assert all(j["state"] == "delivered" for j in jobs(db))
    assert list(uploads.iterdir()) == []


@pytest.mark.parametrize("crash_at", ["before_unlink", "after_unlink"])
def test_actual_cleanup_process_crash_recovers_durable_intent(lifecycle, crash_at):
    _, db, uploads, disk, _ = lifecycle
    job = next(j for j in jobs(db) if j["kind"] == "file")
    disk.objects[job["remote_path"]] = PHOTO
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE delivery_jobs SET state='delivered', delivered_at=? WHERE id=?", (utc_now(), job["id"]))
    child = run_child("""
import os, sys
from pathlib import Path
from delivery import perform_job_cleanup
def before(*_):
    if sys.argv[4] == 'before_unlink': os._exit(74)
original_unlink = Path.unlink
def unlink(path, *a, **kw):
    original_unlink(path, *a, **kw)
    os._exit(74)
if sys.argv[4] == 'after_unlink': Path.unlink = unlink
perform_job_cleanup(Path(sys.argv[1]), sys.argv[2], uploads_dir=Path(sys.argv[3]), pre_unlink_hook=before)
""", db, job["id"], uploads, crash_at)
    assert child.returncode == 74, child.stderr.decode(errors="replace")
    source = Path(job["local_path"])
    assert source.exists() == (crash_at == "before_unlink")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT cleanup_phase FROM delivery_jobs WHERE id=?", (job["id"],)).fetchone()[0] == "deleting"
        conn.execute("UPDATE delivery_jobs SET cleanup_lease_until='2000-01-01T00:00:00+00:00' WHERE id=?", (job["id"],))
    assert process_due_cleanups(db, uploads_dir=uploads) == 1
    assert not source.exists()
    recovered = next(j for j in jobs(db) if j["id"] == job["id"])
    assert recovered["cleanup_state"] == "completed"
    assert recovered["state"] == "delivered"


def test_duplicate_signed_callback_during_failed_delivery_does_not_duplicate_order(lifecycle):
    client, db, uploads, disk, order = lifecycle
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE orders SET payment_status='unpaid',paid_at=NULL WHERE id=?", (order["order_id"],))
    assert client.post("/api/payment/create", json={"order_id": order["order_id"],
                       "order_token": order["order_token"]}).status_code == 200
    signature = hashlib.md5(f"20.00:{order['order_id']}:synthetic-p2".encode()).hexdigest()
    payload = {"InvId": str(order["order_id"]), "OutSum": "20.00", "SignatureValue": signature}
    callback = "/api/payment/robokassa/result"
    assert client.post(callback, data=payload).status_code == 200
    with sqlite3.connect(db) as conn:
        paid = conn.execute("SELECT payment_status, payment_amount, paid_at FROM orders").fetchall()
    disk.failure = "unavailable"
    process_due(db, main.deliver_job, uploads_dir=uploads)
    for _ in range(3):
        assert client.post(callback, data=payload).status_code == 200
    retry(db)
    disk.failure = None
    process_due(db, main.deliver_job, uploads_dir=uploads)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT payment_status, payment_amount, paid_at FROM orders").fetchall() == paid
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM delivery_jobs WHERE kind='paid_email'").fetchone()[0] == 1
    assert len(disk.uploads) == len(set(disk.uploads)) == 2


def test_retention_never_expires_undelivered_or_exhausted_sources(lifecycle):
    _, db, uploads, _, _ = lifecycle
    originals = {p: p.read_bytes() for p in uploads.iterdir()}
    for path in originals:
        os.utime(path, (time.time() - 172800, time.time() - 172800))
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE delivery_jobs SET state='failed', attempts=10")
    assert cleanup_orphaned_temporary_files(uploads, db, max_age_seconds=1) == 0
    assert process_due_cleanups(db, uploads_dir=uploads) == 0
    assert process_due(db, main.deliver_job, uploads_dir=uploads) == 0
    assert {p: p.read_bytes() for p in uploads.iterdir()} == originals
