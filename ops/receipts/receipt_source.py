"""Read-only synthetic A2 reservation contract and physical-file proof.

The schema is a fixture boundary for A1. A2 must supply the production schema
and an independently reviewed schema inventory before enabling this interface.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import platform
import sqlite3
import stat
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from .receipt_protocol import (
    ORDER_FIELDS, ReceiptError, body_digest, canonical_bytes, validate_request,
)

LEGACY_DOMAIN = b"photodoc-legacy-order-ids-v1\x00"
SYS_OPENAT2 = 437  # x86_64 and aarch64 Linux; unsupported platforms fail closed.
RESOLVE_NO_MAGICLINKS = 0x02
RESOLVE_NO_SYMLINKS = 0x04
RESOLVE_BENEATH = 0x08


class OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


def secure_open(root_fd: int, name: str) -> int:
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "aarch64"):
        raise ReceiptError("ROOT_MISMATCH", "openat2 is unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    how = OpenHow(
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        0,
        RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS,
    )
    name_bytes = name.encode("ascii")
    fd = libc.syscall(
        ctypes.c_long(SYS_OPENAT2), ctypes.c_int(root_fd),
        ctypes.c_char_p(name_bytes), ctypes.byref(how), ctypes.sizeof(how),
    )
    if fd < 0:
        raise ReceiptError("ROOT_MISMATCH", f"openat2 rejected source: errno {ctypes.get_errno()}")
    return int(fd)


def secure_root_open(root: Path) -> int:
    return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def legacy_ids_digest(ids: list[int]) -> str:
    if ids != sorted(set(ids)) or any(type(value) is not int or not 0 < value < 2**63 for value in ids):
        raise ReceiptError("INVALID", "legacy ID set")
    data = len(ids).to_bytes(8, "big") + b"".join(value.to_bytes(8, "big") for value in ids)
    return hashlib.sha256(LEGACY_DOMAIN + data).hexdigest()


def schema_digest(conn: sqlite3.Connection) -> str:
    rows = [
        {"type": row[0], "name": row[1], "sql": row[2]}
        for row in conn.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
        )
    ]
    return hashlib.sha256(b"photodoc-app-schema-v1\x00" + canonical_bytes(rows)).hexdigest()


class ReservationVerifier:
    def __init__(
        self, db_path: Path, uploads_root: Path, *, application_id: int,
        instance_uuid: str, epoch_id: str, expected_db_identity: tuple[int, int],
        expected_root_identity: tuple[int, int],
        opener: Callable[[int, str], int] = secure_open,
        root_opener: Callable[[Path], int] = secure_root_open,
    ):
        self.db_path = Path(db_path)
        self.uploads_root = Path(uploads_root)
        self.application_id = application_id
        self.instance_uuid = instance_uuid
        self.epoch_id = epoch_id
        self.expected_db_identity = expected_db_identity
        self.expected_root_identity = expected_root_identity
        self.opener = opener
        self.root_opener = root_opener

    def _connection(self) -> sqlite3.Connection:
        conn = None
        try:
            db_stat = os.stat(self.db_path, follow_symlinks=False)
            if not stat.S_ISREG(db_stat.st_mode) or (db_stat.st_dev, db_stat.st_ino) != self.expected_db_identity:
                raise ReceiptError("SOURCE_MISMATCH", "database identity")
            uri = "file:" + quote(self.db_path.absolute().as_posix(), safe="/:") + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            if conn.execute("PRAGMA application_id").fetchone()[0] != self.application_id:
                raise ReceiptError("SOURCE_MISMATCH", "application ID")
            if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ReceiptError("UNSUPPORTED_VERSION", "fixture schema version")
            return conn
        except ReceiptError:
            if conn is not None:
                conn.close()
            raise
        except (OSError, sqlite3.Error) as exc:
            if conn is not None:
                conn.close()
            raise ReceiptError("SOURCE_MISMATCH", "database unavailable") from exc

    def _epoch(self, conn: sqlite3.Connection, epoch_event: dict) -> None:
        row = conn.execute(
            "SELECT * FROM provenance_epoch WHERE epoch_id=?", (self.epoch_id,)
        ).fetchone()
        if row is None or row["state"] != "ACTIVE":
            raise ReceiptError("SOURCE_MISMATCH", "inactive epoch")
        if row["application_instance_uuid"] != self.instance_uuid:
            raise ReceiptError("SOURCE_MISMATCH", "instance")
        if row["ledger_activation_seq"] != epoch_event["sequence"]:
            raise ReceiptError("SOURCE_MISMATCH", "epoch sequence")
        if row["ledger_activation_hash"] != epoch_event["chain_hash"]:
            raise ReceiptError("SOURCE_MISMATCH", "epoch hash")
        ids = [int(item[0]) for item in conn.execute(
            "SELECT order_id FROM legacy_order_registry ORDER BY order_id"
        )]
        if len(ids) != row["legacy_order_count"] or legacy_ids_digest(ids) != row["legacy_order_ids_sha256"]:
            raise ReceiptError("SOURCE_MISMATCH", "legacy registry")
        payload = epoch_event["payload"]
        if (payload["application_instance_uuid"] != self.instance_uuid
                or payload["epoch_id"] != self.epoch_id
                or payload["legacy_order_count"] != len(ids)
                or payload["legacy_order_ids_sha256"] != legacy_ids_digest(ids)
                or payload["application_id"] != self.application_id
                or payload["user_version"] != 1
                or payload["schema_sha256"] != schema_digest(conn)):
            raise ReceiptError("SOURCE_MISMATCH", "signed epoch")
        high_water = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='orders'"
        ).fetchone()
        if high_water is None or high_water[0] < payload["sqlite_sequence_high_water"]:
            raise ReceiptError("SOURCE_MISMATCH", "SQLite high-water rollback")

    def verify(self, request: dict, passed_fds: list[int], epoch_event: dict) -> None:
        validate_request(request)
        if len(passed_fds) != request["original_count"]:
            raise ReceiptError("INVALID", "FD count")
        if request["application_instance_uuid"] != self.instance_uuid or request["epoch_id"] != self.epoch_id:
            raise ReceiptError("SOURCE_MISMATCH", "instance or epoch")
        conn = self._connection()
        try:
            self._epoch(conn, epoch_event)
            order = conn.execute("SELECT * FROM orders WHERE id=?", (request["order_id"],)).fetchone()
            upload_set = conn.execute(
                "SELECT * FROM order_upload_sets WHERE order_id=?", (request["order_id"],)
            ).fetchone()
            if order is None or upload_set is None:
                raise ReceiptError("SOURCE_MISMATCH", "uncommitted reservation")
            for field in ("order_uuid", "submission_id", "submission_body_digest", "original_count"):
                if upload_set[field] != request[field]:
                    raise ReceiptError("SOURCE_MISMATCH", "reservation " + field)
            if upload_set["state"] != "PREPARING" or upload_set["epoch_id"] != self.epoch_id:
                raise ReceiptError("SOURCE_MISMATCH", "reservation state")
            if not upload_set["submission_key_digest"]:
                raise ReceiptError("SOURCE_MISMATCH", "submission key")
            rows = list(conn.execute(
                "SELECT * FROM order_originals WHERE order_id=? ORDER BY ordinal",
                (request["order_id"],),
            ))
            if len(rows) != request["original_count"]:
                raise ReceiptError("SOURCE_MISMATCH", "published inventory count")
            for ordinal, (item, row) in enumerate(zip(request["files"], rows)):
                if row["ordinal"] != ordinal:
                    raise ReceiptError("SOURCE_MISMATCH", "published ordinal")
                for field in (
                    "upload_id", "ordinal", "client_filename", "display_filename",
                    "stored_name", "relative_path", "media_type", "byte_size",
                    "sha256", "completion_state", "completed_at_utc",
                ):
                    if row[field] != item[field]:
                        raise ReceiptError("SOURCE_MISMATCH", "published " + field)
            form = {key: order[key] for key in ORDER_FIELDS}
            if body_digest(form, request["files"]) != request["submission_body_digest"]:
                raise ReceiptError("SOURCE_MISMATCH", "body digest")

            root_stat = os.stat(self.uploads_root, follow_symlinks=False)
            if not stat.S_ISDIR(root_stat.st_mode) or (root_stat.st_dev, root_stat.st_ino) != self.expected_root_identity:
                raise ReceiptError("ROOT_MISMATCH", "uploads root identity")
            root_fd = self.root_opener(self.uploads_root)
            try:
                opened_root = os.fstat(root_fd)
                if self.opener is secure_open and (
                    opened_root.st_dev, opened_root.st_ino
                ) != self.expected_root_identity:
                    raise ReceiptError("ROOT_MISMATCH", "uploads root changed during open")
                for item, passed in zip(request["files"], passed_fds):
                    fd = self.opener(root_fd, item["stored_name"])
                    try:
                        before = os.fstat(fd)
                        peer = os.fstat(passed)
                        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                                or (before.st_dev, before.st_ino) != (peer.st_dev, peer.st_ino)
                                or before.st_size != item["byte_size"]):
                            raise ReceiptError("SOURCE_MISMATCH", "file identity")
                        digest = hashlib.sha256()
                        while True:
                            chunk = os.read(fd, 1024 * 1024)
                            if not chunk:
                                break
                            digest.update(chunk)
                        after = os.fstat(fd)
                        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                                or digest.hexdigest() != item["sha256"]):
                            raise ReceiptError("SOURCE_MISMATCH", "file hash changed")
                    finally:
                        os.close(fd)
            finally:
                os.close(root_fd)
        except ReceiptError:
            raise
        except (OSError, sqlite3.Error, KeyError, TypeError) as exc:
            raise ReceiptError("SOURCE_MISMATCH", "reservation verification failed") from exc
        finally:
            conn.rollback()
            conn.close()

    def verify_replay(self, request: dict, event: dict, epoch_event: dict) -> None:
        """A historical receipt may replay after legitimate deletion of file bytes."""
        validate_request(request)
        conn = self._connection()
        try:
            self._epoch(conn, epoch_event)
            order = conn.execute("SELECT * FROM orders WHERE id=?", (request["order_id"],)).fetchone()
            upload_set = conn.execute(
                "SELECT * FROM order_upload_sets WHERE order_id=?", (request["order_id"],)
            ).fetchone()
            if order is None or upload_set is None or upload_set["state"] not in ("PREPARING", "ACTIVE"):
                raise ReceiptError("SOURCE_MISMATCH", "replay reservation")
            if upload_set["epoch_id"] != self.epoch_id:
                raise ReceiptError("SOURCE_MISMATCH", "replay epoch")
            for field in ("order_uuid", "submission_id", "submission_body_digest", "original_count"):
                if upload_set[field] != request[field]:
                    raise ReceiptError("SOURCE_MISMATCH", "replay " + field)
            if upload_set["state"] == "ACTIVE":
                if (upload_set["receipt_id"] != event["receipt_id"]
                        or upload_set["receipt_seq"] != event["sequence"]):
                    raise ReceiptError("SOURCE_MISMATCH", "active receipt pointer")
            rows = list(conn.execute(
                "SELECT * FROM order_originals WHERE order_id=? ORDER BY ordinal",
                (request["order_id"],),
            ))
            if len(rows) != request["original_count"]:
                raise ReceiptError("SOURCE_MISMATCH", "replay inventory")
            for ordinal, (row, item) in enumerate(zip(rows, request["files"])):
                if row["ordinal"] != ordinal:
                    raise ReceiptError("SOURCE_MISMATCH", "replay ordinal")
                for field in (
                    "upload_id", "ordinal", "client_filename", "display_filename",
                    "stored_name", "relative_path", "media_type", "byte_size",
                    "sha256", "completed_at_utc",
                ):
                    if row[field] != item[field]:
                        raise ReceiptError("SOURCE_MISMATCH", "replay " + field)
                expected_state = "COMPLETE" if upload_set["state"] == "ACTIVE" else "PUBLISHED"
                if row["completion_state"] != expected_state:
                    raise ReceiptError("SOURCE_MISMATCH", "replay completion")
            if body_digest({key: order[key] for key in ORDER_FIELDS}, request["files"]) != request["submission_body_digest"]:
                raise ReceiptError("SOURCE_MISMATCH", "replay body")
        except ReceiptError:
            raise
        except (OSError, sqlite3.Error, KeyError, TypeError) as exc:
            raise ReceiptError("SOURCE_MISMATCH", "replay verification failed") from exc
        finally:
            conn.rollback()
            conn.close()
