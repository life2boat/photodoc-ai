"""Real-socket SMTP protocol-stage classification and cleanup eligibility.

Every SMTP case below drives an actual implicit-TLS SMTP server over a loopback
socket, so the classified stage comes from the real dialogue rather than from a
stubbed client. The DATA/354 boundary is the only point at which an automatic
retry becomes unsafe, so each stage on both sides of it is covered.

The cleanup tests cover the sweep-window eligibility rule: an unpaid order's
source must be retained *and* must not occupy a slot that a paid cleanup needs.
"""
import datetime
import hashlib
import socket
import sqlite3
import ssl
import sys
import threading
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import delivery  # noqa: E402
import main  # noqa: E402
import paid_email_outbox as email  # noqa: E402


# --------------------------------------------------------------------- fixtures
def _self_signed(directory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "smtp-cert.pem"
    key_path = directory / "smtp-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.db")
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path / "uploads")
    main.UPLOAD_DIR.mkdir()
    for key, value in {
        "DELIVERY_WORKER_ENABLED": "0",
        "PHOTODOC_STAGING": "0",
        "PHOTODOC_ALLOW_FRESH_DB": "0",
        "YANDEX_DISK_ROOT_DIR": "PhotoDoc_Orders",
        "YANDEX_DISK_TOKEN": "synthetic-protocol-stage",
        "ROBOKASSA_PASSWORD2": "synthetic-password2",
        "SMTP_SERVER": "127.0.0.1",
        "SMTP_USER": "robot@example.invalid",
        "SMTP_PASSWORD": "synthetic",
        "EMAIL_TO": "owner@example.invalid",
    }.items():
        monkeypatch.setenv(key, value)
    main.init_db()
    # smtplib resolves the local FQDN on connect; offline that blocks for seconds.
    monkeypatch.setattr(socket, "getfqdn", lambda *args, **kwargs: "localhost")
    return tmp_path


def paid_email_order(oid=1, state="paid"):
    with sqlite3.connect(main.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO orders(id,name,phone,format,paper,crop,comment,payment_status,"
            "order_amount,payment_amount,paid_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (oid, "Synthetic", "+70000000000", "10x15", "Матовая", "Без полей", "",
             state, "20.00", "20.00", "2026-11-04T00:00:00"),
        )
        delivery.enqueue(conn, order_id=oid, kind="paid_email")
    return oid


def outbox_state(oid=1):
    with sqlite3.connect(main.DB_PATH) as conn:
        row = conn.execute(
            "SELECT state,attempts,last_error FROM paid_email_outbox WHERE order_id=?", (oid,)
        ).fetchone()
    return row


def sent_at(oid=1):
    with sqlite3.connect(main.DB_PATH) as conn:
        return conn.execute(
            "SELECT payment_email_sent_at FROM orders WHERE id=?", (oid,)
        ).fetchone()[0]


def insert_delivered_job(conn, oid, payment_status, created_at):
    """Insert a delivered file job whose on-disk bytes match its recorded identity."""
    source = main.UPLOAD_DIR / f"{oid}_source.jpg"
    content = f"synthetic source bytes for order {oid}".encode()
    source.write_bytes(content)
    conn.execute(
        "INSERT INTO orders(id,name,payment_status,order_amount,payment_amount)"
        " VALUES(?,'Synthetic',?,'20.00','20.00')", (oid, payment_status))
    conn.execute(
        "INSERT INTO delivery_jobs(id,order_id,kind,local_path,remote_path,content_sha256,"
        "content_size,state,attempts,next_attempt_at,created_at,delivered_at,cleanup_state,"
        "cleanup_phase) VALUES(?,?,'file',?,?,?,?,'delivered',1,?,?,?,'pending','pending')",
        (f"job-{oid}", oid, str(source), f"PhotoDoc_Orders/Order_{oid}/p.jpg",
         hashlib.sha256(content).hexdigest(), len(content),
         created_at, created_at, created_at))
    return source


# --------------------------------------------------------------- scripted server
class ScriptedSMTPServer(threading.Thread):
    """Implicit-TLS SMTP server that fails or accepts at a chosen protocol stage."""

    daemon = True

    def __init__(self, stage, cert_path, key_path):
        super().__init__()
        self.stage = stage
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(str(cert_path), str(key_path))
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.sessions = 0
        self.body_bytes = 0
        self._stopped = False

    def run(self):
        while not self._stopped:
            try:
                raw, _ = self.sock.accept()
            except OSError:
                return
            self.sessions += 1
            try:
                self._serve(self.ctx.wrap_socket(raw, server_side=True))
            except Exception:
                pass
            finally:
                try:
                    raw.close()
                except OSError:
                    pass

    def _serve(self, conn):
        stream = conn.makefile("rb")
        conn.sendall(b"220 review ESMTP\r\n")
        while True:
            line = stream.readline()
            if not line:
                return
            command = line.strip().upper()
            if command.startswith((b"EHLO", b"HELO")):
                conn.sendall(b"250-review\r\n250-8BITMIME\r\n250 AUTH PLAIN LOGIN\r\n")
            elif command.startswith(b"AUTH"):
                if self.stage == "auth_reject":
                    conn.sendall(b"535 5.7.8 authentication failed\r\n")
                    return
                conn.sendall(b"235 2.7.0 accepted\r\n")
                if self.stage == "drop_after_auth":
                    return                      # dies before MAIL FROM
            elif command.startswith(b"MAIL"):
                conn.sendall(b"250 2.1.0 ok\r\n")
            elif command.startswith(b"RCPT"):
                if self.stage == "drop_on_rcpt":
                    return
                conn.sendall(b"250 2.1.5 ok\r\n")
            elif command.startswith(b"DATA"):
                if self.stage == "reject_data":
                    conn.sendall(b"421 4.3.2 service closing\r\n")
                    return
                if self.stage == "drop_before_data":
                    return                      # dies instead of sending 354
                conn.sendall(b"354 end data with <CR><LF>.<CR><LF>\r\n")
                if self.stage == "drop_in_data":
                    partial = stream.readline()  # body begins, then the socket dies
                    self.body_bytes += len(partial)
                    return
                while True:
                    chunk = stream.readline()
                    if not chunk:
                        return
                    if chunk == b".\r\n":
                        break
                    self.body_bytes += len(chunk)
                if self.stage == "reject_after_data":
                    conn.sendall(b"550 5.7.1 message blocked\r\n")
                    return
                conn.sendall(b"250 2.0.0 queued as review\r\n")
            elif command.startswith(b"QUIT"):
                conn.sendall(b"221 bye\r\n")
                return
            else:
                conn.sendall(b"250 ok\r\n")

    def shutdown(self):
        self._stopped = True
        try:
            self.sock.close()
        except OSError:
            pass


@pytest.fixture
def smtp_server_factory(env, monkeypatch):
    """Start a scripted server and point the application at it."""
    servers = []

    def start(stage):
        cert, key = _self_signed(env)
        server = ScriptedSMTPServer(stage, cert, key)
        server.start()
        servers.append(server)
        monkeypatch.setenv("SMTP_PORT", str(server.port))
        return server

    yield start
    for server in servers:
        server.shutdown()


# ------------------------------------------------------- F1: protocol stage tests
@pytest.mark.parametrize(
    "stage",
    ["drop_after_auth", "drop_on_rcpt", "drop_before_data", "reject_data",
     "reject_after_data", "auth_reject"],
)
def test_failures_before_content_are_retryable(env, smtp_server_factory, stage):
    """Nothing outside the DATA body can deliver a message, so it must retry."""
    oid = paid_email_order()
    smtp_server_factory(stage)

    assert main.send_paid_order_email(oid) is False
    state, attempts, error = outbox_state(oid)
    assert state == "retry", f"{stage} must stay retryable, got {state!r} ({error})"
    assert attempts == 1
    assert sent_at(oid) is None


@pytest.mark.parametrize("stage", ["drop_in_data"])
def test_failures_inside_content_are_uncertain(env, smtp_server_factory, stage):
    """354 was already granted, so acceptance can no longer be disproved."""
    oid = paid_email_order()
    server = smtp_server_factory(stage)

    assert main.send_paid_order_email(oid) is False
    state, attempts, error = outbox_state(oid)
    assert server.body_bytes > 0, "the server must have received part of the body"
    assert state == "uncertain", f"{stage} must stay unproven, got {state!r} ({error})"
    assert sent_at(oid) is None


def test_successful_acceptance_is_recorded_sent(env, smtp_server_factory):
    oid = paid_email_order()
    server = smtp_server_factory("accept")

    assert main.send_paid_order_email(oid) is True
    state, attempts, _ = outbox_state(oid)
    assert state == "sent" and attempts == 1
    assert sent_at(oid) is not None
    assert server.body_bytes > 0


def test_tls_handshake_failure_is_retryable(env, monkeypatch):
    """An implicit-TLS client pointed at a plaintext listener fails in connect."""
    plain = socket.socket()
    plain.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    plain.bind(("127.0.0.1", 0))
    plain.listen(1)
    try:
        monkeypatch.setenv("SMTP_PORT", str(plain.getsockname()[1]))
        oid = paid_email_order()
        assert main.send_paid_order_email(oid) is False
        state, _, error = outbox_state(oid)
        assert state == "retry", f"TLS failure must stay retryable, got {state!r} ({error})"
    finally:
        plain.close()


def test_connection_refused_is_retryable(env, monkeypatch):
    probe = socket.socket()
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()                       # nothing is listening on this port now
    monkeypatch.setenv("SMTP_PORT", str(port))

    oid = paid_email_order()
    assert main.send_paid_order_email(oid) is False
    state, _, error = outbox_state(oid)
    assert state == "retry", f"connection refusal must stay retryable, got {state!r} ({error})"


def test_uncertain_outcome_is_never_automatically_resent(env, smtp_server_factory):
    """The pre-existing duplicate-email guarantee must survive the F1 change."""
    oid = paid_email_order()
    server = smtp_server_factory("drop_in_data")

    assert main.send_paid_order_email(oid) is False
    assert outbox_state(oid)[0] == "uncertain"
    first_sessions = server.sessions

    # a fresh server on the same port is not needed: replay the worker instead
    with sqlite3.connect(main.DB_PATH) as conn:
        conn.execute("UPDATE delivery_jobs SET next_attempt_at=?", (delivery.utc_now(),))
    delivery.process_due(main.DB_PATH, main.deliver_job, uploads_dir=main.UPLOAD_DIR)

    assert server.sessions == first_sessions, "an uncertain attempt was repeated"
    assert outbox_state(oid)[0] == "uncertain"
    assert sent_at(oid) is None


def test_retryable_outcome_is_often_resent_on_the_next_drain(env, smtp_server_factory, monkeypatch):
    """A proven pre-DATA failure must actually become deliverable again."""
    oid = paid_email_order()
    first = smtp_server_factory("drop_after_auth")
    assert main.send_paid_order_email(oid) is False
    assert outbox_state(oid)[0] == "retry"
    first.shutdown()

    cert, key = _self_signed(env)
    healthy = ScriptedSMTPServer("accept", cert, key)
    healthy.start()
    try:
        monkeypatch.setenv("SMTP_PORT", str(healthy.port))
        assert main.send_paid_order_email(oid) is True
    finally:
        healthy.shutdown()
    assert outbox_state(oid)[0] == "sent"
    assert sent_at(oid) is not None


# ------------------------------------------- F2: cleanup sweep eligibility tests
@pytest.mark.parametrize("unpaid_count", [20, 100, 1000])
def test_unpaid_backlog_cannot_starve_paid_cleanup(env, unpaid_count):
    """Unpaid sources are retained but must not consume the sweep window."""
    with sqlite3.connect(main.DB_PATH) as conn:
        unpaid_sources = [
            insert_delivered_job(conn, oid, "unpaid", f"2026-01-01T00:00:00.{oid:06d}+00:00")
            for oid in range(1, unpaid_count + 1)
        ]
        paid_source = insert_delivered_job(
            conn, unpaid_count + 1, "paid", "2026-06-01T00:00:00.000000+00:00")
        conn.commit()

    # production sweep window is 20 (worker_loop -> process_due -> process_due_cleanups)
    cleaned = delivery.process_due_cleanups(main.DB_PATH, uploads_dir=main.UPLOAD_DIR, limit=20)

    assert cleaned == 1, f"expected the paid job to be cleaned, got {cleaned}"
    assert not paid_source.exists(), (
        f"paid cleanup was starved by {unpaid_count} older unpaid jobs")
    with sqlite3.connect(main.DB_PATH) as conn:
        paid_state = conn.execute(
            "SELECT cleanup_state FROM delivery_jobs WHERE order_id=?", (unpaid_count + 1,)
        ).fetchone()[0]
        queued = conn.execute(
            "SELECT COUNT(*) FROM delivery_jobs WHERE state='delivered' AND cleanup_state='pending'"
        ).fetchone()[0]
    assert paid_state == "completed"
    assert queued == unpaid_count, "unpaid jobs stay queued for a later authorised cleanup"
    assert all(source.exists() for source in unpaid_sources), (
        "every unpaid source file must be preserved")


def test_unpaid_jobs_are_not_attempted_by_the_sweep_at_all(env, monkeypatch):
    """Eligibility filtering also removes the futile write-lock churn."""
    with sqlite3.connect(main.DB_PATH) as conn:
        for oid in range(1, 6):
            insert_delivered_job(conn, oid, "unpaid", f"2026-01-01T00:00:0{oid}.000000+00:00")
        conn.commit()

    attempted = []
    real = delivery.perform_job_cleanup

    def counting(db_path, job_id, **kwargs):
        attempted.append(job_id)
        return real(db_path, job_id, **kwargs)

    monkeypatch.setattr(delivery, "perform_job_cleanup", counting)

    assert delivery.process_due_cleanups(main.DB_PATH, uploads_dir=main.UPLOAD_DIR) == 0
    assert attempted == [], (
        f"unpaid jobs must not be attempted at all, got {len(attempted)} attempt(s)")


def test_paid_job_becomes_eligible_once_payment_arrives(env):
    """Retention is a payment gate, not a permanent exclusion."""
    with sqlite3.connect(main.DB_PATH) as conn:
        source = insert_delivered_job(conn, 1, "unpaid", "2026-01-01T00:00:00.000000+00:00")
        conn.commit()

    assert delivery.process_due_cleanups(main.DB_PATH, uploads_dir=main.UPLOAD_DIR) == 0
    assert source.exists()

    with sqlite3.connect(main.DB_PATH) as conn:
        conn.execute("UPDATE orders SET payment_status='paid' WHERE id=1")
        conn.commit()

    assert delivery.process_due_cleanups(main.DB_PATH, uploads_dir=main.UPLOAD_DIR) == 1
    assert not source.exists()


def test_cleanup_still_rechecks_payment_at_the_deletion_boundary(env):
    """The eligibility filter must not replace the in-transaction recheck."""
    with sqlite3.connect(main.DB_PATH) as conn:
        source = insert_delivered_job(conn, 1, "paid", "2026-01-01T00:00:00.000000+00:00")
        conn.commit()

    def revoke_before_unlink(*args, **kwargs):
        with sqlite3.connect(main.DB_PATH) as conn:
            conn.execute("UPDATE orders SET payment_status='unpaid' WHERE id=1")

    assert delivery.perform_job_cleanup(
        main.DB_PATH, "job-1", uploads_dir=main.UPLOAD_DIR, pre_unlink_hook=revoke_before_unlink
    ) is False
    assert source.exists(), "a revocation during cleanup must not delete the source"
