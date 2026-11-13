"""Host-owned, insert-only SQLite receipt ledger and durable local HEAD."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .receipt_protocol import (
    ZERO_HASH, ReceiptError, canonical_bytes, chain_hash, parse_canonical,
    payload_digest, request_digest, signed_bytes, validate_request,
)
from .receipt_source import ReservationVerifier, legacy_ids_digest, schema_digest

SCHEMA = """
CREATE TABLE receipt_events (
 sequence INTEGER PRIMARY KEY CHECK(sequence>0),
 event_type TEXT NOT NULL, instance_uuid TEXT NOT NULL,
 order_id INTEGER, order_uuid TEXT, submission_id TEXT,
 submission_body_digest TEXT, request_digest BLOB,
 payload_jcs BLOB NOT NULL, payload_digest BLOB NOT NULL,
 previous_chain_hash BLOB NOT NULL, chain_hash BLOB NOT NULL,
 key_id TEXT NOT NULL, signature BLOB NOT NULL,
 accepted_at_utc TEXT NOT NULL
);
CREATE TABLE reserved_upload_ids (
 upload_id TEXT PRIMARY KEY,
 accepted_sequence INTEGER NOT NULL REFERENCES receipt_events(sequence)
);
CREATE UNIQUE INDEX receipt_order_once ON receipt_events(instance_uuid,order_id)
 WHERE event_type='UPLOAD_SET_ACCEPTED';
CREATE UNIQUE INDEX receipt_order_uuid_once ON receipt_events(instance_uuid,order_uuid)
 WHERE event_type='UPLOAD_SET_ACCEPTED';
CREATE UNIQUE INDEX receipt_submission_once ON receipt_events(instance_uuid,submission_id)
 WHERE event_type='UPLOAD_SET_ACCEPTED';
CREATE INDEX receipt_request_digest ON receipt_events(request_digest)
 WHERE event_type='UPLOAD_SET_ACCEPTED';
CREATE TRIGGER receipt_no_update BEFORE UPDATE ON receipt_events
 BEGIN SELECT RAISE(ABORT,'receipt events are immutable'); END;
CREATE TRIGGER receipt_no_delete BEFORE DELETE ON receipt_events
 BEGIN SELECT RAISE(ABORT,'receipt events are immutable'); END;
CREATE TRIGGER upload_id_no_update BEFORE UPDATE ON reserved_upload_ids
 BEGIN SELECT RAISE(ABORT,'upload IDs are immutable'); END;
CREATE TRIGGER upload_id_no_delete BEFORE DELETE ON reserved_upload_ids
 BEGIN SELECT RAISE(ABORT,'upload IDs are immutable'); END;
PRAGMA user_version=1;
"""
ROTATE_DOMAIN = b"photodoc-key-rotate-v1\x00"


def rotation_statement(previous: bytes, sequence: int, old_id: str,
                       new_id: str, public_bytes: bytes) -> bytes:
    return (
        ROTATE_DOMAIN + previous + sequence.to_bytes(8, "big")
        + old_id.encode("utf-8") + b"\x00" + new_id.encode("utf-8")
        + b"\x00" + public_bytes
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ReceiptLedger:
    def __init__(
        self, root: Path, *, instance_uuid: str, key_id: str,
        private_key: Ed25519PrivateKey, public_keys: dict[str, Ed25519PublicKey],
        verifier: ReservationVerifier,
        directory_sync: Callable[[Path], None] = sync_directory,
        fault: Callable[[str], None] | None = None,
        export_publisher: Callable[[Path, Path], None] = os.link,
    ):
        self.root = Path(root)
        self.instance_uuid = instance_uuid
        self.key_id = key_id
        self.private_key = private_key
        self.public_keys = public_keys
        self.verifier = verifier
        self.directory_sync = directory_sync
        self.fault = fault or (lambda _: None)
        self.export_publisher = export_publisher
        self._lock = threading.RLock()
        self._unhealthy = False
        if key_id not in public_keys:
            raise ReceiptError("INVALID", "signing key not pinned")
        probe = b"photodoc-key-match"
        try:
            public_keys[key_id].verify(private_key.sign(probe), probe)
        except Exception as exc:
            raise ReceiptError("INVALID", "private/public key mismatch") from exc
        if not self.root.is_dir() or not (self.root / "ledger.sqlite3").is_file():
            raise ReceiptError("STORE_UNAVAILABLE", "explicit store initialization required")
        self.db_path = self.root / "ledger.sqlite3"
        self.head_path = self.root / "HEAD"
        if sys.platform == "linux":
            for path, allowed in (
                (self.root, 0o700), (self.db_path, 0o600), (self.head_path, 0o600),
                (Path(str(self.db_path) + "-wal"), 0o600),
                (Path(str(self.db_path) + "-shm"), 0o600),
            ):
                if path.exists() and stat.S_IMODE(path.stat().st_mode) & ~allowed:
                    raise ReceiptError("STORE_UNAVAILABLE", "ledger permissions too broad")
            if self.root.is_symlink() or self.db_path.is_symlink() or self.head_path.is_symlink():
                raise ReceiptError("STORE_UNAVAILABLE", "ledger path symlink")
        self.conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=10, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._pragmas()
        self.conn.set_authorizer(self._authorize_sql)
        self.verify_startup()

    @staticmethod
    def _authorize_sql(action: int, arg1: str | None, arg2: str | None,
                       db_name: str | None, source: str | None) -> int:
        protected = {"receipt_events", "reserved_upload_ids"}
        if action in (sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE) and arg1 in protected:
            return sqlite3.SQLITE_DENY
        if action in (
            sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ALTER_TABLE,
            sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_INDEX,
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @classmethod
    def initialize(
        cls, root: Path, *, instance_uuid: str,
        directory_sync: Callable[[Path], None] = sync_directory,
    ) -> None:
        """Explicit one-time owner action. Never invoked by service startup."""
        root = Path(root)
        if root.exists() and any(root.iterdir()):
            raise ReceiptError("CONFLICT", "store is not empty")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        db = root / "ledger.sqlite3"
        try:
            conn = sqlite3.connect(db, isolation_level=None)
            try:
                if conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
                    raise ReceiptError("STORE_UNAVAILABLE", "WAL unavailable")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript(SCHEMA)
            finally:
                conn.close()
            os.chmod(db, 0o600)
            head = {
                "version": 1, "instance_uuid": instance_uuid,
                "sequence": 0, "chain_hash": ZERO_HASH.hex(),
            }
            path = root / "HEAD"
            with path.open("xb") as output:
                output.write(canonical_bytes(head))
                output.flush()
                os.fsync(output.fileno())
            os.chmod(path, 0o600)
            directory_sync(root)
            directory_sync(root.parent)
        except Exception as exc:
            # Leave evidence of incomplete initialization for owner inspection.
            raise ReceiptError("IO_UNCERTAIN", "store initialization incomplete") from exc

    def _pragmas(self) -> None:
        if self.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            raise ReceiptError("STORE_UNAVAILABLE", "ledger WAL unavailable")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        if self.conn.execute("PRAGMA synchronous").fetchone()[0] != 2:
            raise ReceiptError("STORE_UNAVAILABLE", "ledger FULL sync unavailable")
        if self.conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise ReceiptError("STORE_UNAVAILABLE", "ledger foreign keys unavailable")
        if self.conn.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ReceiptError("UNSUPPORTED_VERSION", "ledger schema version")

    def _read_head(self) -> dict:
        try:
            raw = self.head_path.read_bytes()
            head = parse_canonical(raw)
            if (frozenset(head) != {
                "version", "instance_uuid", "sequence", "chain_hash"
            } or head["version"] != 1 or head["instance_uuid"] != self.instance_uuid
                    or type(head["sequence"]) is not int or head["sequence"] < 0
                    or type(head["chain_hash"]) is not str
                    or len(bytes.fromhex(head["chain_hash"])) != 32):
                raise ValueError("invalid HEAD")
            return head
        except (OSError, ValueError, ReceiptError) as exc:
            raise ReceiptError("STORE_UNAVAILABLE", "HEAD missing or corrupt") from exc

    def _publish_head(self, sequence: int, chain: bytes) -> None:
        temporary = self.root / (".HEAD-" + uuid.uuid4().hex)
        value = {
            "version": 1, "instance_uuid": self.instance_uuid,
            "sequence": sequence, "chain_hash": chain.hex(),
        }
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(canonical_bytes(value))
                    output.flush()
                    self.fault("head_file_fsync")
                    os.fsync(output.fileno())
            except Exception:
                # fdopen owns fd after construction.
                raise
            self.fault("head_replace")
            os.replace(temporary, self.head_path)
            self.fault("head_directory_fsync")
            self.directory_sync(self.root)
        except Exception as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise ReceiptError("IO_UNCERTAIN", "HEAD publication uncertain") from exc

    def _row_event(self, row: sqlite3.Row) -> dict:
        payload = parse_canonical(bytes(row["payload_jcs"]))
        return {
            "sequence": int(row["sequence"]), "event_type": row["event_type"],
            "payload": payload, "receipt_id": bytes(row["payload_digest"]).hex(),
            "chain_hash": bytes(row["chain_hash"]).hex(),
            "previous_chain_hash": bytes(row["previous_chain_hash"]).hex(),
            "key_id": row["key_id"], "signature": bytes(row["signature"]).hex(),
        }

    def verify_startup(self) -> None:
        with self._lock:
            try:
                if self.conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ReceiptError("STORE_UNAVAILABLE", "ledger integrity")
                objects = {
                    row["name"] for row in self.conn.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table','trigger','index')"
                    )
                }
                required = {
                    "receipt_events", "reserved_upload_ids", "receipt_order_once",
                    "receipt_order_uuid_once", "receipt_submission_once",
                    "receipt_request_digest", "receipt_no_update", "receipt_no_delete",
                    "upload_id_no_update", "upload_id_no_delete",
                }
                if not required <= objects:
                    raise ReceiptError("STORE_UNAVAILABLE", "ledger schema changed")
                previous = ZERO_HASH
                expected = 1
                accepted_ids: dict[str, int] = {}
                epoch_seen = False
                active_key: str | None = None
                hashes = {0: ZERO_HASH}
                for row in self.conn.execute("SELECT * FROM receipt_events ORDER BY sequence"):
                    sequence = row["sequence"]
                    if sequence != expected or row["instance_uuid"] != self.instance_uuid:
                        raise ReceiptError("STORE_UNAVAILABLE", "ledger sequence or instance")
                    payload = parse_canonical(bytes(row["payload_jcs"]))
                    if payload.get("event_type") != row["event_type"]:
                        raise ReceiptError("STORE_UNAVAILABLE", "event type mismatch")
                    if payload.get("accepted_at_utc") != row["accepted_at_utc"]:
                        raise ReceiptError("STORE_UNAVAILABLE", "event timestamp mismatch")
                    if active_key is not None and row["key_id"] != active_key:
                        raise ReceiptError("STORE_UNAVAILABLE", "unexpected signing key")
                    if row["event_type"] == "UPLOAD_SET_ACCEPTED":
                        request = dict(payload)
                        request.pop("accepted_at_utc")
                        validate_request(request)
                        if not epoch_seen:
                            raise ReceiptError("STORE_UNAVAILABLE", "receipt before epoch")
                        if (row["order_id"] != payload["order_id"]
                                or row["order_uuid"] != payload["order_uuid"]
                                or row["submission_id"] != payload["submission_id"]
                                or row["submission_body_digest"] != payload["submission_body_digest"]
                                or bytes(row["request_digest"]) != request_digest(request)):
                            raise ReceiptError("STORE_UNAVAILABLE", "receipt scalar mismatch")
                        for item in payload["files"]:
                            if item["upload_id"] in accepted_ids:
                                raise ReceiptError("STORE_UNAVAILABLE", "duplicate upload ID")
                            accepted_ids[item["upload_id"]] = sequence
                    elif row["event_type"] == "EPOCH_ACTIVATED":
                        if epoch_seen or sequence != 1:
                            raise ReceiptError("STORE_UNAVAILABLE", "duplicate epoch")
                        required_epoch = {
                            "version", "event_type", "application_instance_uuid", "epoch_id",
                            "application_id", "user_version", "schema_sha256",
                            "legacy_order_count", "legacy_order_ids_sha256",
                            "max_order_id", "sqlite_sequence_high_water", "accepted_at_utc",
                        }
                        if (frozenset(payload) != required_epoch or payload["version"] != 1
                                or payload["application_instance_uuid"] != self.instance_uuid
                                or type(payload["application_id"]) is not int
                                or type(payload["user_version"]) is not int
                                or type(payload["legacy_order_count"]) is not int
                                or type(payload["max_order_id"]) is not int
                                or type(payload["sqlite_sequence_high_water"]) is not int
                                or any(row[field] is not None for field in (
                                    "order_id", "order_uuid", "submission_id",
                                    "submission_body_digest", "request_digest",
                                ))):
                            raise ReceiptError("STORE_UNAVAILABLE", "epoch payload")
                        epoch_seen = True
                        active_key = row["key_id"]
                    elif row["event_type"] == "KEY_ROTATED":
                        fields = {
                            "version", "event_type", "application_instance_uuid",
                            "old_key_id", "new_key_id", "new_public_key",
                            "old_proof", "new_proof", "accepted_at_utc",
                        }
                        if (not epoch_seen or frozenset(payload) != fields
                                or payload["version"] != 1
                                or payload["application_instance_uuid"] != self.instance_uuid
                                or payload["old_key_id"] != active_key
                                or any(row[field] is not None for field in (
                                    "order_id", "order_uuid", "submission_id",
                                    "submission_body_digest", "request_digest",
                                ))):
                            raise ReceiptError("STORE_UNAVAILABLE", "rotation payload")
                        new_id = payload["new_key_id"]
                        new_key = self.public_keys.get(new_id)
                        old_key = self.public_keys.get(active_key)
                        if (new_key is None or old_key is None or new_id == active_key):
                            raise ReceiptError("STORE_UNAVAILABLE", "unapproved new key")
                        public_bytes = bytes.fromhex(payload["new_public_key"])
                        pinned = new_key.public_bytes(
                            serialization.Encoding.Raw, serialization.PublicFormat.Raw
                        )
                        if public_bytes != pinned:
                            raise ReceiptError("STORE_UNAVAILABLE", "new key not pinned")
                        statement = rotation_statement(
                            previous, sequence, active_key, new_id, public_bytes
                        )
                        old_key.verify(bytes.fromhex(payload["old_proof"]), statement)
                        new_key.verify(bytes.fromhex(payload["new_proof"]), statement)
                    else:
                        raise ReceiptError("UNSUPPORTED_VERSION", "unsupported event type")
                    digest = payload_digest(payload)
                    if bytes(row["payload_digest"]) != digest or bytes(row["previous_chain_hash"]) != previous:
                        raise ReceiptError("STORE_UNAVAILABLE", "digest or previous link")
                    chain = chain_hash(previous, sequence, digest, row["event_type"], row["key_id"])
                    if bytes(row["chain_hash"]) != chain:
                        raise ReceiptError("STORE_UNAVAILABLE", "chain mismatch")
                    key = self.public_keys.get(row["key_id"])
                    if key is None:
                        raise ReceiptError("STORE_UNAVAILABLE", "unknown signing key")
                    key.verify(
                        bytes(row["signature"]),
                        signed_bytes(previous, sequence, chain, digest, row["event_type"], row["key_id"]),
                    )
                    hashes[sequence] = chain
                    previous = chain
                    expected += 1
                    if row["event_type"] == "KEY_ROTATED":
                        active_key = payload["new_key_id"]
                if epoch_seen and active_key != self.key_id:
                    raise ReceiptError("STORE_UNAVAILABLE", "active signer mismatch")
                actual_ids = {
                    row["upload_id"]: row["accepted_sequence"]
                    for row in self.conn.execute("SELECT * FROM reserved_upload_ids")
                }
                if actual_ids != accepted_ids:
                    raise ReceiptError("STORE_UNAVAILABLE", "upload ID inventory mismatch")
                head = self._read_head()
                if head["sequence"] > expected - 1 or bytes.fromhex(head["chain_hash"]) != hashes.get(head["sequence"]):
                    raise ReceiptError("STORE_UNAVAILABLE", "HEAD inconsistent")
                if head["sequence"] < expected - 1:
                    self._publish_head(expected - 1, previous)
                self._unhealthy = False
            except ReceiptError:
                self._unhealthy = True
                raise
            except Exception as exc:
                self._unhealthy = True
                raise ReceiptError("STORE_UNAVAILABLE", "ledger verification failed") from exc

    def _last(self) -> tuple[int, bytes]:
        row = self.conn.execute(
            "SELECT sequence,chain_hash FROM receipt_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        return (int(row[0]), bytes(row[1])) if row else (0, ZERO_HASH)

    def _assert_live_head(self) -> None:
        if self._unhealthy:
            raise ReceiptError("IO_UNCERTAIN", "ledger requires restart/verification")
        head = self._read_head()
        sequence, chain = self._last()
        if head["sequence"] != sequence or head["chain_hash"] != chain.hex():
            self._unhealthy = True
            raise ReceiptError("IO_UNCERTAIN", "ledger and HEAD diverged")

    def _append(self, payload: dict, *, request: dict | None = None) -> dict:
        self._assert_live_head()
        event_type = payload["event_type"]
        digest = payload_digest(payload)
        req_digest = request_digest(request) if request is not None else None
        sequence, previous = self._last()
        sequence += 1
        chain = chain_hash(previous, sequence, digest, event_type, self.key_id)
        signature = self.private_key.sign(
            signed_bytes(previous, sequence, chain, digest, event_type, self.key_id)
        )
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                """INSERT INTO receipt_events
                (sequence,event_type,instance_uuid,order_id,order_uuid,submission_id,
                 submission_body_digest,request_digest,payload_jcs,payload_digest,
                 previous_chain_hash,chain_hash,key_id,signature,accepted_at_utc)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    sequence, event_type, self.instance_uuid, payload.get("order_id"),
                    payload.get("order_uuid"), payload.get("submission_id"),
                    payload.get("submission_body_digest"), req_digest, canonical_bytes(payload),
                    digest, previous, chain, self.key_id, signature, payload["accepted_at_utc"],
                ),
            )
            if request is not None:
                for item in request["files"]:
                    self.conn.execute(
                        "INSERT INTO reserved_upload_ids(upload_id,accepted_sequence) VALUES (?,?)",
                        (item["upload_id"], sequence),
                    )
            self.fault("before_ledger_commit")
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            raise ReceiptError("CONFLICT", "receipt identity already reserved") from exc
        except Exception as exc:
            try:
                self.conn.rollback()
            except sqlite3.Error:
                pass
            self._unhealthy = True
            raise ReceiptError("IO_UNCERTAIN", "ledger commit uncertain") from exc
        try:
            self.fault("after_ledger_commit")
            self._publish_head(sequence, chain)
        except Exception as exc:
            self._unhealthy = True
            raise ReceiptError("IO_UNCERTAIN", "event committed; HEAD uncertain") from exc
        row = self.conn.execute("SELECT * FROM receipt_events WHERE sequence=?", (sequence,)).fetchone()
        return self._row_event(row)

    def _epoch_event(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM receipt_events WHERE event_type='EPOCH_ACTIVATED'"
        ).fetchone()
        return self._row_event(row) if row else None

    def rotate_key(self, new_key_id: str, new_private_key: Ed25519PrivateKey) -> dict:
        """Host-owner operation; never exposed on backend/audit sockets."""
        with self._lock:
            if (not new_key_id or new_key_id == self.key_id
                    or new_key_id not in self.public_keys):
                raise ReceiptError("INVALID", "new key not pinned or ledger unhealthy")
            self._assert_live_head()
            if self._epoch_event() is None:
                raise ReceiptError("SOURCE_MISMATCH", "epoch absent")
            new_public = new_private_key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
            if new_public != self.public_keys[new_key_id].public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            ):
                raise ReceiptError("INVALID", "new key mismatch")
            sequence, previous = self._last()
            statement = rotation_statement(
                previous, sequence + 1, self.key_id, new_key_id, new_public
            )
            payload = {
                "version": 1, "event_type": "KEY_ROTATED",
                "application_instance_uuid": self.instance_uuid,
                "old_key_id": self.key_id, "new_key_id": new_key_id,
                "new_public_key": new_public.hex(),
                "old_proof": self.private_key.sign(statement).hex(),
                "new_proof": new_private_key.sign(statement).hex(),
                "accepted_at_utc": utc_now(),
            }
            event = self._append(payload)
            self.key_id = new_key_id
            self.private_key = new_private_key
            return event

    def activate_epoch_from_fixture(self) -> dict:
        """A1 synthetic integration seam. Not exposed on the app submit socket."""
        with self._lock:
            if self._epoch_event() is not None:
                return self._epoch_event()
            conn = self.verifier._connection()
            try:
                row = conn.execute(
                    "SELECT * FROM provenance_epoch WHERE epoch_id=?",
                    (self.verifier.epoch_id,),
                ).fetchone()
                if row is None or row["state"] != "PENDING":
                    raise ReceiptError("SOURCE_MISMATCH", "pending synthetic epoch required")
                ids = [int(r[0]) for r in conn.execute("SELECT id FROM orders ORDER BY id")]
                registered = [int(r[0]) for r in conn.execute(
                    "SELECT order_id FROM legacy_order_registry ORDER BY order_id"
                )]
                if (ids != registered or len(ids) != row["legacy_order_count"]
                        or legacy_ids_digest(ids) != row["legacy_order_ids_sha256"]):
                    raise ReceiptError("SOURCE_MISMATCH", "legacy cohort mismatch")
                if row["application_instance_uuid"] != self.instance_uuid:
                    raise ReceiptError("SOURCE_MISMATCH", "epoch instance")
                high_water = conn.execute(
                    "SELECT seq FROM sqlite_sequence WHERE name='orders'"
                ).fetchone()
                if high_water is None:
                    raise ReceiptError("SOURCE_MISMATCH", "SQLite high-water absent")
                payload = {
                    "version": 1, "event_type": "EPOCH_ACTIVATED",
                    "application_instance_uuid": self.instance_uuid,
                    "epoch_id": self.verifier.epoch_id,
                    "application_id": self.verifier.application_id,
                    "user_version": 1,
                    "schema_sha256": schema_digest(conn),
                    "legacy_order_count": len(ids),
                    "legacy_order_ids_sha256": legacy_ids_digest(ids),
                    "max_order_id": max(ids, default=0),
                    "sqlite_sequence_high_water": high_water[0],
                    "accepted_at_utc": utc_now(),
                }
            finally:
                conn.rollback()
                conn.close()
            return self._append(payload)

    def submit(self, request: dict, passed_fds: list[int]) -> dict:
        validate_request(request)
        with self._lock:
            self._assert_live_head()
            epoch = self._epoch_event()
            if epoch is None:
                raise ReceiptError("SOURCE_MISMATCH", "signed epoch absent")
            row = self.conn.execute(
                """SELECT * FROM receipt_events WHERE event_type='UPLOAD_SET_ACCEPTED'
                   AND instance_uuid=? AND submission_id=?""",
                (self.instance_uuid, request["submission_id"]),
            ).fetchone()
            if row is not None:
                event = self._row_event(row)
                projection = dict(event["payload"])
                projection.pop("accepted_at_utc")
                if projection != request or bytes(row["request_digest"]) != request_digest(request):
                    raise ReceiptError("CONFLICT", "changed duplicate submission")
                self.verifier.verify_replay(request, event, epoch)
                return event
            self.verifier.verify(request, passed_fds, epoch)
            payload = dict(request)
            payload["accepted_at_utc"] = utc_now()
            return self._append(payload, request=request)

    def get_by_submission(self, submission_id: str) -> dict | None:
        with self._lock:
            self._assert_live_head()
            row = self.conn.execute(
                """SELECT * FROM receipt_events WHERE event_type='UPLOAD_SET_ACCEPTED'
                   AND instance_uuid=? AND submission_id=?""",
                (self.instance_uuid, submission_id),
            ).fetchone()
            if row is None:
                return None
            event = self._row_event(row)
            projection = dict(event["payload"])
            projection.pop("accepted_at_utc")
            try:
                epoch = self._epoch_event()
                if epoch is None:
                    raise ReceiptError("SOURCE_MISMATCH", "signed epoch absent")
                self.verifier.verify_replay(projection, event, epoch)
            except ReceiptError as exc:
                raise ReceiptError("IO_UNCERTAIN", "receipt reservation unresolved") from exc
            return event

    def audit_export(self, destination: Path) -> dict:
        """No-overwrite, fsynced SQLite snapshot for the separate audit identity."""
        with self._lock:
            self._assert_live_head()
            destination = Path(destination)
            if destination.exists():
                raise ReceiptError("CONFLICT", "audit destination exists")
            temporary = destination.with_name("." + destination.name + "-" + uuid.uuid4().hex)
            try:
                head = self._read_head()
                output = sqlite3.connect(temporary)
                try:
                    self.conn.backup(output)
                    output.execute(
                        "CREATE TABLE export_head(version INTEGER, instance_uuid TEXT,"
                        " sequence INTEGER, chain_hash TEXT)"
                    )
                    output.execute(
                        "INSERT INTO export_head VALUES (?,?,?,?)",
                        (head["version"], head["instance_uuid"], head["sequence"], head["chain_hash"]),
                    )
                    output.commit()
                finally:
                    output.close()
                os.chmod(temporary, 0o600)
                with temporary.open("r+b") as snapshot:
                    os.fsync(snapshot.fileno())
                    snapshot.seek(0)
                    artifact_hash = hashlib.sha256()
                    size = 0
                    while True:
                        chunk = snapshot.read(1024 * 1024)
                        if not chunk:
                            break
                        artifact_hash.update(chunk)
                        size += len(chunk)
                self.export_publisher(temporary, destination)
                self.directory_sync(destination.parent)
                return {
                    "path": str(destination), "head": head,
                    "sha256": artifact_hash.hexdigest(), "size_bytes": size,
                }
            except Exception as exc:
                raise ReceiptError("IO_UNCERTAIN", "audit export incomplete") from exc
            finally:
                temporary.unlink(missing_ok=True)

    def close(self) -> None:
        self.conn.close()
