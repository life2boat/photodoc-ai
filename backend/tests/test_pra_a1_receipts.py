"""Independent synthetic A1 oracle; no application migration or runtime claim."""

from __future__ import annotations

import hashlib
import base64
import json
import os
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import uuid
from array import array
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ops.receipts.receipt_ledger import ReceiptLedger
from ops.receipts.receipt_protocol import (
    ReceiptError, body_digest, canonical_bytes, parse_canonical, validate_request,
)
from ops.receipts.receipt_source import ReservationVerifier, legacy_ids_digest
from ops.receipts.receipt_source import secure_open, secure_root_open
from ops.receipts.receipt_service import (
    ReceiptService, is_unix_listener, resolve_socket_roles,
)

APP_ID = int.from_bytes(b"PDAI", "big")
DOCKER_SUBMIT_UID = 1000
FORM = {
    "name": "Synthetic", "phone": "555", "email": "test@example.invalid",
    "comment": "", "format": "10x15", "paper": "matte", "crop": "none",
}


def ident() -> str:
    return uuid.uuid4().hex


@pytest.fixture
def world(tmp_path: Path):
    root = tmp_path / "receipts"
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / ".synthetic-root-fd").write_bytes(b"fixture only")
    app_db = tmp_path / "app.sqlite3"
    instance, epoch = ident(), ident()
    conn = sqlite3.connect(app_db)
    conn.execute(f"PRAGMA application_id={APP_ID}")
    conn.execute("PRAGMA user_version=1")
    conn.executescript("""
        CREATE TABLE orders (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, phone TEXT,
          email TEXT, comment TEXT, format TEXT, paper TEXT, crop TEXT
        );
        CREATE TABLE provenance_epoch (
          epoch_id TEXT PRIMARY KEY, state TEXT, application_instance_uuid TEXT,
          legacy_order_count INTEGER, legacy_order_ids_sha256 TEXT,
          ledger_activation_seq INTEGER, ledger_activation_hash TEXT
        );
        CREATE TABLE legacy_order_registry (order_id INTEGER PRIMARY KEY);
        CREATE TABLE order_upload_sets (
          order_id INTEGER PRIMARY KEY, epoch_id TEXT, order_uuid TEXT,
          submission_id TEXT, submission_key_digest TEXT,
          submission_body_digest TEXT, original_count INTEGER,
          state TEXT, receipt_id TEXT, receipt_seq INTEGER
        );
        CREATE TABLE order_originals (
          upload_id TEXT PRIMARY KEY, order_id INTEGER, ordinal INTEGER,
          client_filename TEXT, display_filename TEXT, stored_name TEXT,
          relative_path TEXT, media_type TEXT, byte_size INTEGER, sha256 TEXT,
          completion_state TEXT, completed_at_utc TEXT
        );
    """)
    conn.execute(
        "INSERT INTO orders(id,name,phone,email,comment,format,paper,crop)"
        " VALUES (1,?,?,?,?,?,?,?)", tuple(FORM.values()),
    )
    conn.execute("INSERT INTO legacy_order_registry(order_id) VALUES (1)")
    conn.execute(
        "INSERT INTO provenance_epoch VALUES (?,?,?,?,?,?,?)",
        (epoch, "PENDING", instance, 1, legacy_ids_digest([1]), None, None),
    )
    conn.commit()
    conn.close()
    key = Ed25519PrivateKey.generate()
    ReceiptLedger.initialize(root, instance_uuid=instance, directory_sync=lambda _: None)
    verifier = ReservationVerifier(
        app_db, uploads, application_id=APP_ID, instance_uuid=instance,
        epoch_id=epoch,
        expected_db_identity=(app_db.stat().st_dev, app_db.stat().st_ino),
        expected_root_identity=(uploads.stat().st_dev, uploads.stat().st_ino),
        # Explicit synthetic opener: Linux openat2 is a separate runtime gate.
        opener=lambda _, name: os.open(uploads / name, os.O_RDONLY),
        root_opener=lambda root: os.open(root / ".synthetic-root-fd", os.O_RDONLY),
    )

    def open_ledger(fault=None):
        def synthetic_export(source: Path, target: Path):
            with source.open("rb") as input_file, target.open("xb") as output:
                output.write(input_file.read())
                output.flush()
                os.fsync(output.fileno())

        return ReceiptLedger(
            root, instance_uuid=instance, key_id="test-key", private_key=key,
            public_keys={"test-key": key.public_key()}, verifier=verifier,
            directory_sync=lambda _: None, fault=fault,
            export_publisher=synthetic_export,
        )

    ledger = open_ledger()
    epoch_event = ledger.activate_epoch_from_fixture()
    ledger.close()
    conn = sqlite3.connect(app_db)
    conn.execute(
        "UPDATE provenance_epoch SET state='ACTIVE',ledger_activation_seq=?,"
        "ledger_activation_hash=? WHERE epoch_id=?",
        (epoch_event["sequence"], epoch_event["chain_hash"], epoch),
    )
    conn.commit()
    conn.close()
    yield {
        "root": root, "uploads": uploads, "db": app_db, "instance": instance,
        "epoch": epoch, "open": open_ledger, "verifier": verifier, "key": key,
    }


def add_order(world: dict, *, data: bytes = b"synthetic-original-A", upload_id: str | None = None):
    conn = sqlite3.connect(world["db"])
    cursor = conn.execute(
        "INSERT INTO orders(name,phone,email,comment,format,paper,crop)"
        " VALUES (?,?,?,?,?,?,?)", tuple(FORM.values()),
    )
    order_id = cursor.lastrowid
    upload_id = upload_id or ident()
    stored = f"{order_id}_{upload_id}.jpg"
    path = world["uploads"] / stored
    path.write_bytes(data)
    file = {
        "upload_id": upload_id, "ordinal": 0, "client_filename": "photo.jpg",
        "display_filename": "photo.jpg", "stored_name": stored,
        "relative_path": "uploads/" + stored, "media_type": "image/jpeg",
        "byte_size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "completion_state": "PUBLISHED",
        "completed_at_utc": "2026-11-06T00:00:00.000000Z",
    }
    request = {
        "version": 1, "event_type": "UPLOAD_SET_ACCEPTED",
        "application_instance_uuid": world["instance"], "epoch_id": world["epoch"],
        "submission_id": ident(), "submission_body_digest": body_digest(FORM, [file]),
        "order_uuid": ident(), "order_id": order_id,
        "original_count": 1, "files": [file],
    }
    conn.execute(
        "INSERT INTO order_upload_sets VALUES (?,?,?,?,?,?,?,?,?,?)",
        (order_id, world["epoch"], request["order_uuid"], request["submission_id"],
         hashlib.sha256(request["submission_id"].encode()).hexdigest(),
         request["submission_body_digest"], 1, "PREPARING", None, None),
    )
    conn.execute(
        "INSERT INTO order_originals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            upload_id, order_id, 0, file["client_filename"], file["display_filename"],
            stored, file["relative_path"], file["media_type"], len(data),
            file["sha256"], "PUBLISHED", file["completed_at_utc"],
        ),
    )
    conn.commit()
    conn.close()
    return request, path


def submit(ledger: ReceiptLedger, request: dict, path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY)
    try:
        return ledger.submit(request, [fd])
    finally:
        os.close(fd)


def assert_error(code: str, callback):
    with pytest.raises(ReceiptError) as found:
        callback()
    assert found.value.code == code


def test_receipt_signed_chain_and_exact_replay(world):
    request, path = add_order(world)
    ledger = world["open"]()
    first = submit(ledger, request, path)
    assert first["sequence"] == 2
    assert first["receipt_id"] == hashlib.sha256(
        b"photodoc-receipt-event-v1\x00UPLOAD_SET_ACCEPTED\x00"
        + canonical_bytes(first["payload"])
    ).hexdigest()
    conn = sqlite3.connect(world["db"])
    conn.execute(
        "UPDATE order_upload_sets SET state='ACTIVE',receipt_id=?,receipt_seq=? WHERE order_id=?",
        (first["receipt_id"], first["sequence"], request["order_id"]),
    )
    conn.execute(
        "UPDATE order_originals SET completion_state='COMPLETE' WHERE order_id=?",
        (request["order_id"],),
    )
    conn.commit()
    conn.close()
    path.unlink()
    assert ledger.submit(request, []) == first  # Historical replay needs no deleted bytes.
    assert ledger.get_by_submission(request["submission_id"]) == first
    assert ledger._last()[0] == 2
    ledger.close()
    reopened = world["open"]()
    assert reopened.get_by_submission(request["submission_id"]) == first
    reopened.close()


def test_adversarial_reservation_and_physical_mismatch(world):
    request, path = add_order(world)
    ledger = world["open"]()
    changed = dict(request, order_uuid=ident())
    assert_error("SOURCE_MISMATCH", lambda: submit(ledger, changed, path))
    changed = dict(request, submission_id=ident())
    assert_error("SOURCE_MISMATCH", lambda: submit(ledger, changed, path))
    path.write_bytes(b"synthetic-original-B")
    assert_error("SOURCE_MISMATCH", lambda: submit(ledger, request, path))
    path.write_bytes(b"synthetic-original-A")
    other = world["uploads"] / "different"
    other.write_bytes(b"synthetic-original-A")
    assert_error("SOURCE_MISMATCH", lambda: submit(ledger, request, other))
    assert ledger._last()[0] == 1
    ledger.close()


def test_duplicate_submission_conflicts_and_insert_only(world):
    request, path = add_order(world)
    ledger = world["open"]()
    submit(ledger, request, path)
    changed = dict(request, order_uuid=ident())
    assert_error("CONFLICT", lambda: ledger.submit(changed, []))
    with pytest.raises(sqlite3.DatabaseError):
        ledger.conn.execute("UPDATE receipt_events SET event_type='EVIL' WHERE sequence=2")
    with pytest.raises(sqlite3.DatabaseError):
        ledger.conn.execute("DELETE FROM receipt_events WHERE sequence=2")
    row = ledger.conn.execute("SELECT * FROM receipt_events WHERE sequence=2").fetchone()
    columns = [column[1] for column in ledger.conn.execute("PRAGMA table_info(receipt_events)")]
    copied = dict(zip(columns, row))
    copied["sequence"] = 3
    copied["submission_id"] = ident()
    placeholders = ",".join("?" for _ in columns)
    with pytest.raises(sqlite3.IntegrityError):
        ledger.conn.execute(
            f"INSERT INTO receipt_events({','.join(columns)}) VALUES ({placeholders})",
            tuple(copied[column] for column in columns),
        )
    with pytest.raises(sqlite3.IntegrityError):
        ledger.conn.execute(
            "INSERT INTO reserved_upload_ids VALUES (?,?)",
            (request["files"][0]["upload_id"], 2),
        )
    ledger.close()


def test_missing_fd_and_schema_or_epoch_drift_fail_closed(world):
    request, path = add_order(world)
    ledger = world["open"]()
    assert_error("INVALID", lambda: ledger.submit(request, []))
    conn = sqlite3.connect(world["db"])
    conn.execute("DELETE FROM legacy_order_registry WHERE order_id=1")
    conn.commit()
    conn.close()
    assert_error("SOURCE_MISMATCH", lambda: submit(ledger, request, path))
    ledger.close()


def test_unknown_fixture_schema_refused(world):
    request, path = add_order(world)
    conn = sqlite3.connect(world["db"])
    conn.execute("ALTER TABLE order_originals ADD COLUMN unknown_original TEXT")
    conn.commit()
    conn.close()
    ledger = world["open"]()
    assert_error("SOURCE_MISMATCH", lambda: submit(ledger, request, path))
    ledger.close()


def test_lookup_refuses_ledger_only_receipt_after_app_rollback(world):
    request, path = add_order(world)
    ledger = world["open"]()
    submit(ledger, request, path)
    conn = sqlite3.connect(world["db"])
    conn.execute("DELETE FROM order_originals WHERE order_id=?", (request["order_id"],))
    conn.execute("DELETE FROM order_upload_sets WHERE order_id=?", (request["order_id"],))
    conn.execute("DELETE FROM orders WHERE id=?", (request["order_id"],))
    conn.commit()
    conn.close()
    assert_error(
        "IO_UNCERTAIN", lambda: ledger.get_by_submission(request["submission_id"])
    )
    ledger.close()


@pytest.mark.parametrize("stage", [
    "before_ledger_commit", "after_ledger_commit", "head_file_fsync",
    "head_replace", "head_directory_fsync",
])
def test_crash_injection_never_acknowledges_uncertainty(world, stage):
    request, path = add_order(world)

    def fail(point):
        if point == stage:
            raise OSError("synthetic failure at " + stage)

    ledger = world["open"](fault=fail)
    assert_error("IO_UNCERTAIN", lambda: submit(ledger, request, path))
    assert_error("IO_UNCERTAIN", lambda: ledger.get_by_submission(request["submission_id"]))
    ledger.close()
    reopened = world["open"]()  # Full verification repairs only a lagging HEAD.
    existing = reopened.get_by_submission(request["submission_id"])
    if stage == "before_ledger_commit":
        assert existing is None
        assert submit(reopened, request, path)["sequence"] == 2
    else:
        assert existing is not None and existing["sequence"] == 2
        assert reopened.submit(request, []) == existing
    reopened.close()


def test_corrupt_event_and_head_halt_without_reset(world):
    request, path = add_order(world)
    ledger = world["open"]()
    submit(ledger, request, path)
    ledger.close()
    conn = sqlite3.connect(world["root"] / "ledger.sqlite3")
    conn.execute("DROP TRIGGER receipt_no_update")  # Privileged synthetic tamper.
    conn.execute("UPDATE receipt_events SET signature=? WHERE sequence=2", (bytes(64),))
    conn.commit()
    conn.close()
    assert_error("STORE_UNAVAILABLE", world["open"])
    assert (world["root"] / "ledger.sqlite3").exists()


def test_head_ahead_is_not_auto_reset(world):
    ledger = world["open"]()
    ledger.close()
    head = world["root"] / "HEAD"
    value = json.loads(head.read_text())
    value["sequence"] = 99
    head.write_bytes(canonical_bytes(value))
    assert_error("STORE_UNAVAILABLE", world["open"])


def test_live_head_divergence_blocks_lookup_and_new_receipt(world):
    request, path = add_order(world)
    ledger = world["open"]()
    submit(ledger, request, path)
    head = world["root"] / "HEAD"
    value = json.loads(head.read_text())
    value["sequence"] = 1
    value["chain_hash"] = ledger.conn.execute(
        "SELECT hex(chain_hash) FROM receipt_events WHERE sequence=1"
    ).fetchone()[0].lower()
    head.write_bytes(canonical_bytes(value))
    assert_error(
        "IO_UNCERTAIN", lambda: ledger.get_by_submission(request["submission_id"])
    )
    assert_error("IO_UNCERTAIN", lambda: ledger.submit(request, []))
    ledger.close()
    repaired = world["open"]()
    assert repaired.get_by_submission(request["submission_id"]) is not None
    repaired.close()


def test_audit_export_no_overwrite(world, tmp_path):
    request, path = add_order(world)
    ledger = world["open"]()
    submit(ledger, request, path)
    dest = tmp_path / "export.sqlite3"
    report = ledger.audit_export(dest)
    assert report["head"]["sequence"] == 2
    conn = sqlite3.connect(dest)
    assert conn.execute("SELECT count(*) FROM receipt_events").fetchone()[0] == 2
    assert conn.execute("SELECT sequence FROM export_head").fetchone()[0] == 2
    conn.close()
    assert_error("CONFLICT", lambda: ledger.audit_export(dest))
    ledger.close()


def test_signed_dual_proof_key_rotation(world):
    new_key = Ed25519PrivateKey.generate()
    common = {
        "instance_uuid": world["instance"], "verifier": world["verifier"],
        "public_keys": {
            "test-key": world["key"].public_key(),
            "next-key": new_key.public_key(),
        },
        "directory_sync": lambda _: None,
    }
    old = ReceiptLedger(
        world["root"], key_id="test-key", private_key=world["key"], **common
    )
    event = old.rotate_key("next-key", new_key)
    assert event["event_type"] == "KEY_ROTATED"
    old.close()
    reopened = ReceiptLedger(
        world["root"], key_id="next-key", private_key=new_key, **common
    )
    request, path = add_order(world)
    assert submit(reopened, request, path)["sequence"] == 3
    reopened.close()


def test_canonical_parser_rejects_ambiguous_values():
    assert parse_canonical(b'{"a":1,"b":"x"}') == {"a": 1, "b": "x"}
    for raw in (
        b'{"b":"x","a":1}', b'{"a":1,"a":2}', b'{"a":1.0}',
        b'{"a":NaN}', b'{"a":"\\ud800"}',
    ):
        assert_error("INVALID", lambda raw=raw: parse_canonical(raw))


@pytest.mark.skipif(sys.platform != "linux", reason="real openat2 requires Linux")
def test_linux_openat2_confinement_and_file_identity(world):
    world["verifier"].opener = secure_open
    world["verifier"].root_opener = secure_root_open
    request, path = add_order(world)
    ledger = world["open"]()
    alias = world["uploads"] / "alias"
    os.link(path, alias)
    assert_error("SOURCE_MISMATCH", lambda: submit(ledger, request, path))
    alias.unlink()
    wrong = world["uploads"] / "wrong"
    wrong.write_bytes(path.read_bytes())
    assert_error("SOURCE_MISMATCH", lambda: submit(ledger, request, wrong))
    assert submit(ledger, request, path)["sequence"] == 2
    root_fd = secure_root_open(world["uploads"])
    try:
        for name in ("../escape", "/etc/passwd", "alias"):
            assert_error("ROOT_MISMATCH", lambda name=name: secure_open(root_fd, name))
        symlink = world["uploads"] / "alias"
        symlink.symlink_to(path)
        assert_error("ROOT_MISMATCH", lambda: secure_open(root_fd, "alias"))
    finally:
        os.close(root_fd)
    ledger.close()


@pytest.mark.skipif(sys.platform != "linux", reason="SCM_RIGHTS/SO_PEERCRED requires Linux")
def test_linux_socket_fds_and_peer_identity(world, tmp_path):
    world["verifier"].opener = secure_open
    world["verifier"].root_opener = secure_root_open
    request, path = add_order(world)
    ledger = world["open"]()
    service = ReceiptService(
        ledger, submit_uid=os.getuid(), audit_uid=os.getuid(), export_dir=tmp_path,
    )
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    thread = threading.Thread(target=service.handle, args=(server, "submit"))
    thread.start()
    message = canonical_bytes({
        "version": 1, "operation": "SubmitUploadSetV1", "request": request,
    })
    fd = os.open(path, os.O_RDONLY)
    try:
        client.sendmsg(
            [struct.pack("!I", len(message))],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array("i", [fd]))],
        )
        client.sendall(message)
        length = struct.unpack("!I", client.recv(4))[0]
        response = parse_canonical(client.recv(length))
        assert response["ok"] is True
        assert response["result"]["sequence"] == 2
    finally:
        os.close(fd)
        client.close()
        thread.join(timeout=5)
        ledger.close()


@pytest.mark.skipif(sys.platform != "linux", reason="real audit publication requires Linux")
def test_linux_audit_hardlink_publication_and_fsync(world, tmp_path):
    from ops.receipts.receipt_ledger import ReceiptLedger

    ledger = ReceiptLedger(
        world["root"], instance_uuid=world["instance"], key_id="test-key",
        private_key=world["key"],
        public_keys={"test-key": world["key"].public_key()},
        verifier=world["verifier"],
    )
    destination = tmp_path / "real-export.sqlite3"
    report = ledger.audit_export(destination)
    assert report["head"]["sequence"] == 1
    assert destination.is_file()
    assert_error("CONFLICT", lambda: ledger.audit_export(destination))
    ledger.close()


@pytest.mark.skipif(sys.platform != "linux", reason="audit FD transfer requires Linux")
def test_linux_audit_socket_returns_private_snapshot_fd(world, tmp_path):
    ledger = world["open"]()
    service = ReceiptService(
        ledger, submit_uid=os.getuid(), audit_uid=os.getuid(), export_dir=tmp_path,
    )
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    thread = threading.Thread(target=service.handle, args=(server, "audit"))
    thread.start()
    message = canonical_bytes({"version": 1, "operation": "AuditExportV1"})
    client.sendall(struct.pack("!I", len(message)) + message)
    received_fd = None
    try:
        header, ancillary, _, _ = client.recvmsg(
            4, socket.CMSG_SPACE(struct.calcsize("i")),
        )
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                received_fd = struct.unpack("i", data[:4])[0]
        assert received_fd is not None
        length = struct.unpack("!I", header)[0]
        body = bytearray()
        while len(body) < length:
            body.extend(client.recv(length - len(body)))
        response = parse_canonical(bytes(body))
        assert response["ok"] is True
        chunks = []
        while True:
            piece = os.read(received_fd, 1024 * 1024)
            if not piece:
                break
            chunks.append(piece)
        snapshot_bytes = b"".join(chunks)
        assert hashlib.sha256(snapshot_bytes).hexdigest() == response["result"]["sha256"]
        assert len(snapshot_bytes) == response["result"]["size_bytes"]
        recovered = tmp_path / "audit-oracle.sqlite3"
        recovered.write_bytes(snapshot_bytes)
        check = sqlite3.connect(recovered)
        assert check.execute("SELECT sequence FROM export_head").fetchone()[0] == 1
        check.close()
    finally:
        if received_fd is not None:
            os.close(received_fd)
        client.close()
        thread.join(timeout=5)
        ledger.close()


@pytest.mark.skipif(
    sys.platform != "linux" or os.getenv("PRA_A1_DOCKER_TEST") != "1",
    reason="explicit isolated Linux Docker runtime gate",
)
def test_linux_docker_bind_mount_fd_and_uid(world, tmp_path):
    assert shutil.which("docker")
    world["verifier"].opener = secure_open
    world["verifier"].root_opener = secure_root_open
    request, path = add_order(world)
    ledger = world["open"]()
    service = ReceiptService(
        ledger, submit_uid=DOCKER_SUBMIT_UID, audit_uid=os.getuid(), export_dir=tmp_path,
    )
    os.chmod(tmp_path, 0o755)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    socket_path = tmp_path / "submit.sock"
    listener.bind(str(socket_path))
    os.chown(socket_path, os.getuid(), DOCKER_SUBMIT_UID)
    os.chmod(socket_path, 0o660)
    listener.listen(2)
    result: list[Exception] = []

    def serve_one():
        try:
            connection, _ = listener.accept()
            service.handle(connection, "submit")
        except Exception as exc:
            result.append(exc)

    thread = threading.Thread(target=serve_one)
    thread.start()
    message = canonical_bytes({
        "version": 1, "operation": "SubmitUploadSetV1", "request": request,
    })
    client_code = """
import array,base64,json,os,socket,struct,sys
body=base64.b64decode(sys.argv[1]); path=sys.argv[2]
s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
s.connect('/run/receipts/submit.sock')
fd=os.open('/files/'+path,os.O_RDONLY)
st=os.fstat(fd)
s.sendmsg([struct.pack('!I',len(body))],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[fd]))])
s.sendall(body)
header=s.recv(4); n=struct.unpack('!I',header)[0]; response=b''
while len(response)<n: response+=s.recv(n-len(response))
print(json.dumps({'uid':os.getuid(),'gid':os.getgid(),'dev':st.st_dev,'ino':st.st_ino,'response':json.loads(response)}))
os.close(fd); s.close()
"""
    command = [
        "docker", "run", "--rm", "--pull=never", "--network=none",
        "--user", f"{DOCKER_SUBMIT_UID}:{DOCKER_SUBMIT_UID}",
        "--mount", f"type=bind,src={world['uploads']},dst=/files,readonly",
        "--mount", f"type=bind,src={tmp_path},dst=/run/receipts",
        "python:3.11-slim", "python", "-c", client_code,
        base64.b64encode(message).decode(), path.name,
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=45)
        assert completed.returncode == 0, completed.stderr
        evidence = json.loads(completed.stdout.strip())
        assert evidence["uid"] == DOCKER_SUBMIT_UID
        assert (evidence["dev"], evidence["ino"]) == (path.stat().st_dev, path.stat().st_ino)
        assert evidence["response"]["ok"] is True, evidence
        assert evidence["response"]["result"]["sequence"] == 2
        assert not result
    finally:
        listener.close()
        thread.join(timeout=5)
        ledger.close()


@pytest.mark.skipif(
    sys.platform != "linux" or os.getenv("PRA_A1_DOCKER_TEST") != "1",
    reason="explicit isolated Linux Docker runtime gate",
)
def test_linux_docker_named_volume_fd_and_inode(world, tmp_path):
    assert shutil.which("docker")
    request, path = add_order(world)
    name = "pra-a1-" + uuid.uuid4().hex
    subprocess.run(["docker", "volume", "create", name], check=True, capture_output=True)
    try:
        mountpoint = Path(subprocess.run(
            ["docker", "volume", "inspect", "--format", "{{.Mountpoint}}", name],
            check=True, capture_output=True, text=True,
        ).stdout.strip())
        assert mountpoint.is_dir()
        copy_code = "import shutil,sys; shutil.copyfile('/source/'+sys.argv[1],'/files/'+sys.argv[1])"
        subprocess.run([
            "docker", "run", "--rm", "--pull=never", "--network=none",
            "--mount", f"type=bind,src={world['uploads']},dst=/source,readonly",
            "--mount", f"type=volume,src={name},dst=/files",
            "python:3.11-slim", "python", "-c", copy_code, path.name,
        ], check=True, capture_output=True, timeout=45)
        world["verifier"].uploads_root = mountpoint
        world["verifier"].expected_root_identity = (
            mountpoint.stat().st_dev, mountpoint.stat().st_ino,
        )
        world["verifier"].opener = secure_open
        world["verifier"].root_opener = secure_root_open
        ledger = world["open"]()
        service = ReceiptService(
            ledger, submit_uid=DOCKER_SUBMIT_UID, audit_uid=os.getuid(), export_dir=tmp_path,
        )
        os.chmod(tmp_path, 0o755)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        socket_path = tmp_path / "submit-named.sock"
        listener.bind(str(socket_path))
        os.chown(socket_path, os.getuid(), DOCKER_SUBMIT_UID)
        os.chmod(socket_path, 0o660)
        listener.listen(1)
        result: list[Exception] = []

        def serve_one():
            try:
                connection, _ = listener.accept()
                service.handle(connection, "submit")
            except Exception as exc:
                result.append(exc)

        thread = threading.Thread(target=serve_one)
        thread.start()
        message = canonical_bytes({
            "version": 1, "operation": "SubmitUploadSetV1", "request": request,
        })
        code = """
import array,base64,json,os,socket,struct,sys
b=base64.b64decode(sys.argv[1]); f=os.open('/files/'+sys.argv[2],os.O_RDONLY)
s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.connect('/run/receipts/submit-named.sock')
st=os.fstat(f); s.sendmsg([struct.pack('!I',len(b))],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[f]))])
s.sendall(b); n=struct.unpack('!I',s.recv(4))[0]; r=b''
while len(r)<n: r+=s.recv(n-len(r))
print(json.dumps({'dev':st.st_dev,'ino':st.st_ino,'response':json.loads(r)}))
os.close(f); s.close()
"""
        try:
            completed = subprocess.run([
                "docker", "run", "--rm", "--pull=never", "--network=none",
                "--user", f"{DOCKER_SUBMIT_UID}:{DOCKER_SUBMIT_UID}",
                "--mount", f"type=volume,src={name},dst=/files,readonly",
                "--mount", f"type=bind,src={tmp_path},dst=/run/receipts",
                "python:3.11-slim", "python", "-c", code,
                base64.b64encode(message).decode(), path.name,
            ], capture_output=True, text=True, timeout=45)
            assert completed.returncode == 0, completed.stderr
            evidence = json.loads(completed.stdout.strip())
            host_file = mountpoint / path.name
            assert (evidence["dev"], evidence["ino"]) == (
                host_file.stat().st_dev, host_file.stat().st_ino,
            )
            assert evidence["response"]["ok"] is True, evidence
            assert evidence["response"]["result"]["sequence"] == 2
            assert not result
        finally:
            listener.close()
            thread.join(timeout=5)
            ledger.close()
    finally:
        subprocess.run(["docker", "volume", "rm", name], check=True, capture_output=True)


@pytest.mark.skipif(
    sys.platform != "linux" or os.getenv("PRA_A1_DOCKER_TEST") != "1",
    reason="explicit isolated Linux Docker runtime gate",
)
def test_linux_docker_container_root_rejected_by_peer_check(world, tmp_path):
    ledger = world["open"]()
    service = ReceiptService(
        ledger, submit_uid=DOCKER_SUBMIT_UID, audit_uid=os.getuid(), export_dir=tmp_path,
    )
    os.chmod(tmp_path, 0o755)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    endpoint = tmp_path / "root-probe.sock"
    listener.bind(str(endpoint))
    os.chown(endpoint, os.getuid(), DOCKER_SUBMIT_UID)
    os.chmod(endpoint, 0o660)
    listener.listen(1)
    thread = threading.Thread(
        target=lambda: service.handle(listener.accept()[0], "submit"),
    )
    thread.start()
    code = """
import json,socket,struct
s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.connect('/run/receipts/root-probe.sock')
body=b'{"operation":"GetBySubmissionIdV1","submission_id":"00000000000040008000000000000000","version":1}'
s.sendall(struct.pack('!I',len(body))+body)
n=struct.unpack('!I',s.recv(4))[0]; response=b''
while len(response)<n: response+=s.recv(n-len(response))
print(response.decode()); s.close()
"""
    try:
        completed = subprocess.run([
            "docker", "run", "--rm", "--pull=never", "--network=none",
            "--user", "0:0",
            "--mount", f"type=bind,src={tmp_path},dst=/run/receipts",
            "python:3.11-slim", "python", "-c", code,
        ], capture_output=True, text=True, timeout=45)
        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout)["error_code"] == "INVALID"
    finally:
        listener.close()
        thread.join(timeout=5)
        ledger.close()


# ---------------------------------------------------------------- F-1: socket role mapping
UNIT_DIR = Path(__file__).resolve().parents[2] / "ops" / "receipts" / "systemd"


def _roles(names: str, *, count: str = "2", pid: int | None = None,
           is_listener=lambda fd: True):
    return resolve_socket_roles(str(os.getpid() if pid is None else pid), count, names,
                                is_listener=is_listener)


def test_socket_roles_resolve_by_name_in_either_order():
    """systemd may hand over submit:audit or audit:submit; the role must follow the name."""
    forward = _roles("submit:audit")
    reverse = _roles("audit:submit")
    assert forward == {"submit": 3, "audit": 4}
    assert reverse == {"audit": 3, "submit": 4}
    # A positional implementation would put audit on fd 3 in the reversed case.
    assert reverse["submit"] == 4 and reverse["audit"] == 3


def test_socket_roles_reject_duplicate_names():
    assert_error("STORE_UNAVAILABLE", lambda: _roles("submit:submit"))
    assert_error("STORE_UNAVAILABLE", lambda: _roles("audit:audit"))


def test_socket_roles_reject_unexpected_or_missing_name():
    assert_error("STORE_UNAVAILABLE", lambda: _roles("submit:admin"))
    assert_error("STORE_UNAVAILABLE", lambda: _roles("submit:"))
    assert_error("STORE_UNAVAILABLE", lambda: _roles(":audit"))
    assert_error("STORE_UNAVAILABLE", lambda: _roles("submit"))


def test_socket_roles_reject_wrong_pid_or_count():
    assert_error("STORE_UNAVAILABLE", lambda: _roles("submit:audit", pid=os.getpid() + 1))
    assert_error("STORE_UNAVAILABLE", lambda: _roles("submit:audit", count="1"))
    assert_error("STORE_UNAVAILABLE", lambda: _roles("submit:audit", count="3"))
    assert_error("STORE_UNAVAILABLE", lambda: _roles("submit:audit", count="two"))
    assert_error("STORE_UNAVAILABLE", lambda: _roles("submit:audit", count=""))


def test_socket_roles_reject_non_listening_descriptor():
    """A descriptor that is not a listening Unix socket must never become a role."""
    assert_error("STORE_UNAVAILABLE",
                 lambda: _roles("submit:audit", is_listener=lambda fd: fd == 3))
    assert_error("STORE_UNAVAILABLE",
                 lambda: _roles("audit:submit", is_listener=lambda fd: False))


def test_is_unix_listener_rejects_a_plain_file_descriptor(tmp_path):
    target = tmp_path / "not-a-socket"
    target.write_bytes(b"x")
    fd = os.open(target, os.O_RDONLY)
    try:
        assert is_unix_listener(fd) is False
    finally:
        os.close(fd)


# ------------------------------------------------------- F-2/F-3: unit provisioning
def test_socket_units_allow_group_traversal():
    """The parent directory must be traversable, while the node stays 0660 per group."""
    for name in ("photodoc-receipts-submit.socket", "photodoc-receipts-audit.socket"):
        text = (UNIT_DIR / name).read_text(encoding="utf-8")
        assert "DirectoryMode=0755" in text, name
        assert "SocketMode=0660" in text, name
        assert "DirectoryMode=0777" not in text, name
        assert "SocketMode=0666" not in text, name


def test_service_unit_allows_the_audit_export_directory():
    """ProtectSystem=strict must stay, and only receipts+exports may be writable."""
    text = (UNIT_DIR / "photodoc-receipts.service").read_text(encoding="utf-8")
    assert "ProtectSystem=strict" in text
    writable = [line.split("=", 1)[1].split()
                for line in text.splitlines() if line.startswith("ReadWritePaths=")]
    assert writable, "ReadWritePaths missing"
    paths = set(writable[0])
    assert "/var/lib/photodoc/receipts" in paths
    assert "/var/lib/photodoc/exports" in paths
    assert len(paths) == 2, paths
