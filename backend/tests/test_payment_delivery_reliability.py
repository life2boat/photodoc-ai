"""Payment gates and durable SMTP outcomes using synthetic HTTP/SMTP and real SQLite."""
import hashlib
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
import delivery
import paid_email_outbox as email
import sqlite_busy


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.db")
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path / "uploads")
    main.UPLOAD_DIR.mkdir()
    for key, value in {"DELIVERY_WORKER_ENABLED":"0", "PHOTODOC_STAGING":"0", "PHOTODOC_ALLOW_FRESH_DB":"0",
                       "YANDEX_DISK_ROOT_DIR":"PhotoDoc_Orders", "YANDEX_DISK_TOKEN":"synthetic-payment-gate",
                       "ROBOKASSA_PASSWORD2":"synthetic-password2", "SMTP_SERVER":"smtp.example.invalid",
                       "SMTP_USER":"test@example.invalid", "SMTP_PASSWORD":"synthetic", "EMAIL_TO":"owner@example.invalid"}.items():
        monkeypatch.setenv(key, value)
    main.init_db()
    def forbidden(*args, **kwargs):
        pytest.fail("Unexpected live transport")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(main.smtplib, "SMTP_SSL", forbidden)
    return tmp_path


def order(state="unpaid", oid=1, kind="file"):
    source = main.UPLOAD_DIR / f"{oid}_synthetic.jpg"
    content = b"synthetic retained source"
    source.write_bytes(content)
    with sqlite3.connect(main.DB_PATH) as c:
        c.execute("INSERT INTO orders(id,name,payment_status,order_amount,payment_amount) VALUES(?,'Synthetic',?,'20.00','20.00')", (oid, state))
        jid = delivery.enqueue(c, order_id=oid, kind=kind, local_path=str(source),
            remote_path=f"PhotoDoc_Orders/Order_{oid}/photo.jpg", sha256=hashlib.sha256(content).hexdigest(), size=len(content))
    return source, jid


def job(jid):
    with sqlite3.connect(main.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        return dict(c.execute("SELECT * FROM delivery_jobs WHERE id=?", (jid,)).fetchone())


def email_state(oid=1):
    with sqlite3.connect(main.DB_PATH) as c:
        return c.execute("SELECT state,attempts FROM paid_email_outbox WHERE order_id=?", (oid,)).fetchone()


def drain():
    return delivery.process_due(main.DB_PATH, main.deliver_job, uploads_dir=main.UPLOAD_DIR)


class SMTP:
    sends = 0
    hook = None
    def __init__(self, *args, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def login(self, *args): pass
    def send_message(self, message):
        type(self).sends += 1
        if type(self).hook: type(self).hook()
        return {}


@pytest.fixture
def smtp(env, monkeypatch):
    SMTP.sends = 0
    SMTP.hook = None
    monkeypatch.setattr(main.smtplib, "SMTP_SSL", SMTP)
    return SMTP


@pytest.mark.parametrize("state", ["unpaid", "pending", "failed", "cancelled", None])
@pytest.mark.parametrize("kind", ["file", "info"])
def test_unpaid_never_claims_uploads_or_deletes_even_legacy_delivered(env, state, kind):
    source, jid = order(state, kind=kind)
    assert delivery.claim_due(main.DB_PATH) is None
    drain()
    assert source.read_bytes() == b"synthetic retained source" and job(jid)["attempts"] == 0
    with pytest.raises(RuntimeError, match="paid"):
        main.deliver_job(job(jid))
    with sqlite3.connect(main.DB_PATH) as c:
        c.execute("UPDATE delivery_jobs SET state='delivered',cleanup_phase='deleting' WHERE id=?", (jid,))
    assert not delivery.perform_job_cleanup(main.DB_PATH, jid, uploads_dir=main.UPLOAD_DIR)
    assert source.is_file() and job(jid)["local_deleted_at"] is None


def test_claim_execution_and_cleanup_recheck_payment(env, monkeypatch):
    source, jid = order("paid")
    claimed = delivery.claim_due(main.DB_PATH)
    with sqlite3.connect(main.DB_PATH) as c:
        c.execute("UPDATE orders SET payment_status='unpaid'")
    with pytest.raises(RuntimeError, match="paid"): main.deliver_job(claimed)
    assert not delivery.finish(main.DB_PATH, jid, lease_until=claimed["lease_until"])
    with sqlite3.connect(main.DB_PATH) as c:
        c.execute("UPDATE orders SET payment_status='paid'")
        c.execute("UPDATE delivery_jobs SET state='delivered'")
    def revoke(*args):
        with sqlite3.connect(main.DB_PATH) as c: c.execute("UPDATE orders SET payment_status='unpaid'")
    assert not delivery.perform_job_cleanup(main.DB_PATH, jid, uploads_dir=main.UPLOAD_DIR, pre_unlink_hook=revoke)
    assert source.is_file() and job(jid)["local_deleted_at"] is None


def test_payment_revoked_during_upload_preparation_prevents_file_put(env, monkeypatch):
    source, jid = order("paid")
    puts = []
    def get(url, **kw):
        if url.endswith("/upload"):
            with sqlite3.connect(main.DB_PATH) as c: c.execute("UPDATE orders SET payment_status='failed'")
            return httpx.Response(200, json={"href":"https://upload.example.invalid/file"}, request=httpx.Request("GET", url))
        status, data = (200, {"type":"dir"}) if kw["params"].get("fields") == "type" else (404, {})
        return httpx.Response(status, json=data, request=httpx.Request("GET", url))
    monkeypatch.setattr(main.httpx, "get", get)
    monkeypatch.setattr(main.httpx, "put", lambda *a, **k: puts.append(a))
    with pytest.raises(RuntimeError, match="paid"): main.deliver_job(job(jid))
    assert puts == [] and source.is_file()


def callback(client, amount="20.00", oid=1, valid=True):
    sig = hashlib.md5(f"{amount}:{oid}:synthetic-password2".encode()).hexdigest()
    return client.post("/api/payment/robokassa/result", data={"OutSum":amount, "InvId":str(oid), "SignatureValue":sig if valid else "wrong"})


def test_verified_callback_releases_same_legacy_jobs_and_duplicate_is_idempotent(env, monkeypatch):
    source, jid = order()
    with TestClient(main.app) as client:
        assert callback(client, valid=False).status_code == 400
        assert delivery.claim_due(main.DB_PATH) is None
        assert callback(client).status_code == 200
        assert callback(client).status_code == 200
    with sqlite3.connect(main.DB_PATH) as c:
        assert c.execute("SELECT COUNT(*) FROM delivery_jobs WHERE kind='paid_email'").fetchone()[0] == 1
    claimed = delivery.claim_due(main.DB_PATH)
    assert claimed and source.is_file()
    # Explicitly finish the same file job, proving cleanup requires paid state.
    with sqlite3.connect(main.DB_PATH) as c:
        c.execute("UPDATE delivery_jobs SET state='processing',lease_until='synthetic' WHERE id=?", (jid,))
    assert delivery.finish(main.DB_PATH, jid, lease_until="synthetic")
    assert delivery.perform_job_cleanup(main.DB_PATH, jid, uploads_dir=main.UPLOAD_DIR)
    assert not source.exists()


def test_unpaid_api_order_keeps_both_sources_after_restarts_and_failed_payment(env, monkeypatch):
    monkeypatch.setattr(main, "send_order_email", lambda *a: True)
    with TestClient(main.app) as client:
        response = client.post("/api/order", data={"name":"Synthetic", "phone":"79990000000", "format":"10x15"},
            files={"files":("synthetic.jpg", b"\xff\xd8\xffsynthetic", "image/jpeg")})
        assert response.status_code == 200
        oid = response.json()["order_id"]
        original = {p.name:p.read_bytes() for p in main.UPLOAD_DIR.iterdir()}
        assert len(original) == 2
        assert callback(client, amount="10.00", oid=oid).status_code == 400
        for _ in range(3):
            main.init_db()  # Same durable queue after process/startup recovery.
            drain()
        with sqlite3.connect(main.DB_PATH) as c:
            assert c.execute("SELECT state,attempts,local_deleted_at FROM delivery_jobs WHERE kind IN ('file','info')").fetchall() == [("pending",0,None)] * 2
        assert {p.name:p.read_bytes() for p in main.UPLOAD_DIR.iterdir()} == original


@pytest.mark.parametrize("amount", ["abc", "NaN", "inf", "-1", "0", "1e10000", "20..00"])
def test_signed_malformed_outsum_is_controlled_400(env, amount):
    order()
    with TestClient(main.app) as client:
        assert callback(client, amount).status_code == 400
    assert main.get_order_by_id(1)["payment_status"] == "unpaid"


def test_paid_email_lock_after_smtp_acceptance_retries_sql_not_smtp(smtp):
    order("paid", kind="paid_email")
    released = threading.Event()
    def hold_after_send():
        c = sqlite3.connect(main.DB_PATH, check_same_thread=False)
        c.execute("BEGIN IMMEDIATE")
        def release():
            c.rollback(); c.close(); released.set()
        threading.Timer(0.45, release).start()
    smtp.hook = hold_after_send
    drain()
    assert released.wait(2)
    drain()
    assert smtp.sends == 1 and email_state() == ("sent", 1)
    assert main.get_order_by_id(1)["payment_email_sent_at"]


def test_uncommittable_acknowledgment_never_automatically_resends(smtp, monkeypatch):
    _, jid = order("paid", kind="paid_email")
    real_record = email.record
    def unavailable(*args, **kwargs): raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(email, "record", unavailable)
    drain()
    monkeypatch.setattr(email, "record", real_record)
    with sqlite3.connect(main.DB_PATH) as c: c.execute("UPDATE delivery_jobs SET next_attempt_at=?", (delivery.utc_now(),))
    drain(); drain()
    assert smtp.sends == 1 and email_state()[0] == "uncertain"
    assert job(jid)["state"] != "delivered"


@pytest.mark.parametrize("failure", ["before_data", "ambiguous_data", "rejected_data"])
def test_email_failure_states(smtp, monkeypatch, failure):
    order("paid", kind="paid_email")
    if failure == "before_data":
        monkeypatch.setattr(SMTP, "login", lambda *a: (_ for _ in ()).throw(TimeoutError()))
    elif failure == "ambiguous_data":
        smtp.hook = lambda: (_ for _ in ()).throw(TimeoutError())
    else:
        smtp.hook = lambda: (_ for _ in ()).throw(main.smtplib.SMTPDataError(451, b"synthetic rejection"))
    drain()
    assert email_state()[0] == ("uncertain" if failure == "ambiguous_data" else "retry")
    assert main.get_order_by_id(1)["payment_email_sent_at"] is None
    monkeypatch.setattr(SMTP, "login", lambda *a: None)
    smtp.hook = None
    with sqlite3.connect(main.DB_PATH) as c:
        c.execute("UPDATE delivery_jobs SET next_attempt_at=?", (delivery.utc_now(),))
    drain()
    assert smtp.sends == (2 if failure == "rejected_data" else 1)
    assert email_state()[0] == ("uncertain" if failure == "ambiguous_data" else "sent")


def test_smtp_quit_failure_after_accepted_data_is_not_a_retry(smtp, monkeypatch):
    order("paid", kind="paid_email")
    monkeypatch.setattr(SMTP, "__exit__", lambda *a: (_ for _ in ()).throw(TimeoutError()))
    drain(); main.init_db(); drain()
    assert smtp.sends == 1 and email_state() == ("sent", 1)


def test_multiple_workers_and_direct_senders_send_once(smtp):
    order("paid", kind="paid_email")
    smtp.hook = lambda: time.sleep(0.08)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: main.send_paid_order_email(1), range(12)))
    assert smtp.sends == 1 and email_state() == ("sent", 1)


@pytest.mark.parametrize("stage", ["before_data", "after_data"])
def test_actual_process_crash_after_smtp_acceptance_requires_reconciliation(smtp, env, stage):
    _, jid = order("paid", kind="paid_email")
    receipt = env / "smtp-receipt"
    code = '''
import os,sys,dotenv
dotenv.load_dotenv=lambda *a,**k:False
import main
class SMTP:
 def __init__(self,*a,**k):pass
 def __enter__(self):return self
 def __exit__(self,*a):pass
 def login(self,*a):
  if sys.argv[2]=='before_data':os._exit(73)
 def send_message(self,msg):
  with open(sys.argv[1],'wb') as f:f.write(b'accepted');f.flush();os.fsync(f.fileno())
  os._exit(73)
main.smtplib.SMTP_SSL=SMTP
from delivery import process_due
process_due(main.DB_PATH,main.deliver_job,limit=1)
'''
    child_env = {**os.environ, "DATABASE_PATH":str(main.DB_PATH), "PYTHONPATH":str(Path(main.__file__).parent), "DELIVERY_WORKER_ENABLED":"0"}
    result = subprocess.run([sys.executable, "-B", "-c", code, str(receipt), stage], env=child_env, capture_output=True, timeout=20)
    assert result.returncode == 73, result.stderr.decode(errors="replace")
    assert receipt.exists() == (stage == "after_data") and email_state()[0] == "sending"
    with sqlite3.connect(main.DB_PATH) as c: c.execute("UPDATE delivery_jobs SET lease_until='2000-01-01' WHERE id=?", (jid,))
    drain()
    assert smtp.sends == 0 and email_state()[0] == "uncertain"


@pytest.mark.parametrize("attempts,state,expected", [(0,"pending","pending"),(1,"failed","uncertain"),(1,"processing","uncertain"),(1,"delivered","uncertain")])
def test_legacy_email_migration_preserves_unknown_outcomes(env, attempts, state, expected):
    _, jid = order("paid", kind="paid_email")
    with sqlite3.connect(main.DB_PATH) as c:
        c.execute("DELETE FROM paid_email_outbox")
        c.execute("UPDATE delivery_jobs SET state=?,attempts=? WHERE id=?", (state, attempts, jid))
    email.initialize(main.DB_PATH); email.initialize(main.DB_PATH)
    assert email_state()[0] == expected


@pytest.mark.parametrize("operation", ["claim", "finish"])
def test_real_sqlite_contention_is_bounded_and_recovers(env, operation):
    _, jid = order("paid")
    claimed = delivery.claim_due(main.DB_PATH) if operation == "finish" else None
    c = sqlite3.connect(main.DB_PATH, check_same_thread=False); c.execute("BEGIN IMMEDIATE")
    def release(): c.rollback(); c.close()
    timer = threading.Timer(0.4, release); timer.start()
    try:
        result = delivery.claim_due(main.DB_PATH) if operation == "claim" else delivery.finish(main.DB_PATH, jid, lease_until=claimed["lease_until"])
    finally: timer.join()
    assert result


def test_non_busy_errors_not_retried_and_busy_budget_expires(env, monkeypatch):
    calls = []
    @sqlite_busy.retry_busy
    def invalid():
        calls.append(1); raise sqlite3.OperationalError("no such table: synthetic")
    with pytest.raises(sqlite3.OperationalError): invalid()
    assert len(calls) == 1
    order("paid")
    monkeypatch.setattr(sqlite_busy, "RETRY_BUDGET", 0.2)
    with sqlite3.connect(main.DB_PATH) as c:
        c.execute("BEGIN IMMEDIATE")
        start = time.monotonic()
        with pytest.raises(sqlite3.OperationalError): delivery.claim_due(main.DB_PATH)
        assert time.monotonic() - start < 0.8


def test_extended_sqlite_busy_codes_are_recognized():
    error = sqlite3.OperationalError("synthetic")
    error.sqlite_errorcode = 517  # SQLITE_BUSY_SNAPSHOT
    assert sqlite_busy.is_busy(error)
    error.sqlite_errorcode = 19  # SQLITE_CONSTRAINT
    assert not sqlite_busy.is_busy(error)
