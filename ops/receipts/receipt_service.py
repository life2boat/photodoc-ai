"""Two-role, versioned Unix socket service for the A1 host receipt writer."""

from __future__ import annotations

import argparse
import json
import os
import selectors
import socket
import stat
import struct
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .receipt_ledger import ReceiptLedger
from .receipt_protocol import MAX_REQUEST, ReceiptError, canonical_bytes, parse_canonical
from .receipt_source import ReservationVerifier
from .receipt_protocol import UUID4

HEADER = struct.Struct("!I")
PEER = struct.Struct("3i")
FD_STRUCT = struct.Struct("i")


def _read_exact(connection: socket.socket, length: int) -> bytes:
    output = bytearray()
    while len(output) < length:
        chunk = connection.recv(length - len(output))
        if not chunk:
            raise ReceiptError("INVALID", "truncated request")
        output.extend(chunk)
    return bytes(output)


def _request(connection: socket.socket) -> tuple[dict, list[int]]:
    fds: list[int] = []
    try:
        flags = getattr(socket, "MSG_CMSG_CLOEXEC", 0)
        header, ancillary, message_flags, _ = connection.recvmsg(
            HEADER.size, socket.CMSG_SPACE(10 * FD_STRUCT.size), flags,
        )
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                fds.extend(struct.unpack(f"{len(data) // FD_STRUCT.size}i", data))
        if message_flags & getattr(socket, "MSG_CTRUNC", 0):
            raise ReceiptError("INVALID", "truncated FD list")
        if not header:
            raise ReceiptError("INVALID", "empty frame")
        if len(header) < HEADER.size:
            header += _read_exact(connection, HEADER.size - len(header))
        length = HEADER.unpack(header)[0]
        if not 0 < length <= MAX_REQUEST:
            raise ReceiptError("INVALID", "request size")
        return parse_canonical(_read_exact(connection, length)), fds
    except Exception:
        for fd in fds:
            os.close(fd)
        raise


def _send(connection: socket.socket, value: dict) -> None:
    body = canonical_bytes(value)
    if len(body) > MAX_REQUEST:
        raise ReceiptError("INVALID", "response too large")
    connection.sendall(HEADER.pack(len(body)) + body)


def _send_with_fd(connection: socket.socket, value: dict, fd: int) -> None:
    body = canonical_bytes(value)
    if len(body) > MAX_REQUEST:
        raise ReceiptError("INVALID", "response too large")
    header = HEADER.pack(len(body))
    written = connection.sendmsg(
        [header],
        [(socket.SOL_SOCKET, socket.SCM_RIGHTS, FD_STRUCT.pack(fd))],
    )
    if written < len(header):
        connection.sendall(header[written:])
    connection.sendall(body)


def peer_identity(connection: socket.socket) -> tuple[int, int, int]:
    if not hasattr(socket, "SO_PEERCRED"):
        raise ReceiptError("STORE_UNAVAILABLE", "SO_PEERCRED unavailable")
    data = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, PEER.size)
    return PEER.unpack(data)


class ReceiptService:
    def __init__(
        self, ledger: ReceiptLedger, *, submit_uid: int, audit_uid: int,
        export_dir: Path,
    ):
        self.ledger = ledger
        self.submit_uid = submit_uid
        self.audit_uid = audit_uid
        self.export_dir = Path(export_dir)

    def handle(self, connection: socket.socket, role: str) -> None:
        connection.settimeout(10)
        fds: list[int] = []
        try:
            _, uid, _ = peer_identity(connection)
            if uid != (self.submit_uid if role == "submit" else self.audit_uid):
                raise ReceiptError("INVALID", "unauthorized peer")
            message, fds = _request(connection)
            if frozenset(message) not in (
                {"version", "operation", "request"},
                {"version", "operation", "submission_id"},
                {"version", "operation"},
            ) or type(message.get("version")) is not int or message["version"] != 1:
                raise ReceiptError("UNSUPPORTED_VERSION", "protocol envelope")
            operation = message["operation"]
            if role == "submit" and operation == "SubmitUploadSetV1":
                if frozenset(message) != {"version", "operation", "request"}:
                    raise ReceiptError("INVALID", "submit envelope")
                result = self.ledger.submit(message["request"], fds)
            elif role == "submit" and operation == "GetBySubmissionIdV1":
                if frozenset(message) != {"version", "operation", "submission_id"} or fds:
                    raise ReceiptError("INVALID", "lookup envelope")
                if type(message["submission_id"]) is not str or not UUID4.fullmatch(message["submission_id"]):
                    raise ReceiptError("INVALID", "submission ID")
                result = self.ledger.get_by_submission(message["submission_id"])
            elif role == "audit" and operation == "AuditExportV1":
                if frozenset(message) != {"version", "operation"} or fds:
                    raise ReceiptError("INVALID", "audit envelope")
                if not self.export_dir.is_dir():
                    raise ReceiptError("STORE_UNAVAILABLE", "audit directory")
                result = self.ledger.audit_export(
                    self.export_dir / ("audit-" + os.urandom(16).hex() + ".sqlite3")
                )
                export_fd = os.open(result["path"], os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
                try:
                    _send_with_fd(
                        connection,
                        {"ok": True, "result": {key: value for key, value in result.items() if key != "path"}},
                        export_fd,
                    )
                finally:
                    os.close(export_fd)
                return
            else:
                raise ReceiptError("INVALID", "operation not allowed on this socket")
            _send(connection, {"ok": True, "result": result})
        except ReceiptError as exc:
            try:
                _send(connection, {"ok": False, "error_code": exc.code})
            except (OSError, ReceiptError):
                pass
        except (OSError, ValueError, TypeError):
            try:
                _send(connection, {"ok": False, "error_code": "IO_UNCERTAIN"})
            except (OSError, ReceiptError):
                pass
        finally:
            for fd in fds:
                os.close(fd)
            connection.close()

    def serve_inherited(self) -> None:
        if os.environ.get("LISTEN_PID") != str(os.getpid()):
            raise ReceiptError("STORE_UNAVAILABLE", "systemd socket activation required")
        if os.environ.get("LISTEN_FDS") != "2":
            raise ReceiptError("STORE_UNAVAILABLE", "both sockets required")
        if os.environ.get("LISTEN_FDNAMES") != "submit:audit":
            raise ReceiptError("STORE_UNAVAILABLE", "socket roles ambiguous")
        selector = selectors.DefaultSelector()
        listeners = []
        try:
            for fd, role in ((3, "submit"), (4, "audit")):
                listener = socket.socket(fileno=fd)
                listener.setblocking(False)
                listeners.append(listener)
                selector.register(listener, selectors.EVENT_READ, role)
            while True:
                for key, _ in selector.select():
                    connection, _ = key.fileobj.accept()
                    self.handle(connection, key.data)
        finally:
            selector.close()
            for listener in listeners:
                listener.close()


def from_config(config_path: Path) -> ReceiptService:
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    expected = {
        "ledger_root", "export_dir", "app_db", "uploads_root", "application_id",
        "instance_uuid", "epoch_id", "db_dev", "db_ino", "uploads_dev",
        "uploads_ino", "private_key_file", "public_key_files", "key_id",
        "submit_uid", "audit_uid",
    }
    if frozenset(config) != expected:
        raise ReceiptError("INVALID", "config fields")
    key_path = Path(config["private_key_file"])
    key_stat = key_path.stat()
    if stat.S_IMODE(key_stat.st_mode) & 0o077:
        raise ReceiptError("INVALID", "signing key permissions")
    private = Ed25519PrivateKey.from_private_bytes(key_path.read_bytes())
    if type(config["public_key_files"]) is not dict or not config["public_key_files"]:
        raise ReceiptError("INVALID", "public key registry")
    public_keys = {
        key_id: Ed25519PublicKey.from_public_bytes(Path(path).read_bytes())
        for key_id, path in config["public_key_files"].items()
    }
    verifier = ReservationVerifier(
        Path(config["app_db"]), Path(config["uploads_root"]),
        application_id=config["application_id"],
        instance_uuid=config["instance_uuid"], epoch_id=config["epoch_id"],
        expected_db_identity=(config["db_dev"], config["db_ino"]),
        expected_root_identity=(config["uploads_dev"], config["uploads_ino"]),
    )
    ledger = ReceiptLedger(
        Path(config["ledger_root"]), instance_uuid=config["instance_uuid"],
        key_id=config["key_id"], private_key=private,
        public_keys=public_keys, verifier=verifier,
    )
    return ReceiptService(
        ledger, submit_uid=config["submit_uid"],
        audit_uid=config["audit_uid"], export_dir=Path(config["export_dir"]),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    from_config(args.config).serve_inherited()


if __name__ == "__main__":
    main()
