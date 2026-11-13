"""Helpers that build realistic synthetic backup sets for the backup tests.

Not a test module (no ``test_`` prefix) so pytest does not collect it.

Data is created through the application's **own** endpoints — ``POST /api/order``
and the signed Robokassa Result callback — instead of hand-written INSERTs. That
matters: the project's verifier enforces P1 conventions (stored filename format,
exactly one ``info`` job per order, durable cleanup evidence), so a fixture built
by hand would exercise a schema that never occurs in production.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
SCRIPTS_DIR = REPO_DIR / "scripts"
for _p in (str(BACKEND_DIR), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

JPEG = b"\xff\xd8\xffsynthetic-jpeg-data-for-backup-rehearsal"
PASSWORD2 = "synthetic-pass-2"

#: order number -> (filename sent by the client, bytes)
CUSTOMER_FILES = {
    1: ("pending_photo.jpg", JPEG + b"-1"),
    2: ("processing_photo.jpg", JPEG + b"-2"),
    3: ("delivered_photo.jpg", JPEG + b"-3"),
    4: ("failed_photo.jpg", JPEG + b"-4"),
    5: ("unpaid_photo.jpg", JPEG + b"-5"),
}

#: order number -> outbox state to force after the payment callback
OUTBOX_TARGET = {1: "sending", 2: "uncertain", 3: "sent", 4: "pending"}


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_app():
    import main
    return main


def build_database(
    db_path: Path,
    uploads_dir: Path,
    *,
    with_outbox: bool = True,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Create orders 1-5 with every job state, paid/unpaid mix and outbox states."""
    from fastapi.testclient import TestClient

    main = _load_app()
    now = now or datetime.now(timezone.utc)
    db_path, uploads_dir = Path(db_path), Path(uploads_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    uploads_dir.mkdir(parents=True, exist_ok=True)

    import consents

    original_approval = {
        p: consents.DOCUMENTS[p].get("approved")
        for p in consents.MANDATORY_PURPOSES
    }
    saved = {name: os.environ.get(name) for name in
              ("ROBOKASSA_MERCHANT_LOGIN", "ROBOKASSA_PASSWORD1", "ROBOKASSA_PASSWORD2",
              "DELIVERY_WORKER_ENABLED", "YANDEX_DISK_ROOT_DIR", "SMTP_SERVER", "SMTP_USER",
              "SMTP_PASSWORD", "EMAIL_TO", "CONSENT_FEATURE_ENABLED")}
    original = (main.DB_PATH, main.UPLOAD_DIR)
    try:
        for p in consents.MANDATORY_PURPOSES:
            consents.DOCUMENTS[p]["approved"] = True

        os.environ.update({
            "ROBOKASSA_MERCHANT_LOGIN": "synthetic-merchant",
            "ROBOKASSA_PASSWORD1": "synthetic-pass-1",
            "ROBOKASSA_PASSWORD2": PASSWORD2,
            "DELIVERY_WORKER_ENABLED": "0",
            "YANDEX_DISK_ROOT_DIR": "PhotoDoc_Synthetic",
            "CONSENT_FEATURE_ENABLED": "1",
        })
        for name in ("SMTP_SERVER", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"):
            os.environ.pop(name, None)

        main.DB_PATH, main.UPLOAD_DIR = db_path, uploads_dir
        main.init_db()

        client = TestClient(main.app)
        order_ids: Dict[int, int] = {}
        for index, (customer_name, data) in CUSTOMER_FILES.items():
            response = client.post(
                "/api/order",
                data={
                    "name": f"Synthetic {index}",
                    "phone": "+79990000000",
                    "format": "10x15",
                    "paper": "Matte",
                    "crop": "None",
                    "consent_service_terms": "true",
                    "consent_personal_data": "true",
                },
                files=[("files", (customer_name, data, "image/jpeg"))],
            )
            assert response.status_code == 200, response.text
            order_ids[index] = response.json()["order_id"]

        # Pay orders 1-4 through the real signed callback so the paid_email job and
        # the durable outbox row are created exactly as in production.
        for index in (1, 2, 3, 4):
            order_id = order_ids[index]
            signature = hashlib.md5(
                f"20.00:{order_id}:{PASSWORD2}".encode()).hexdigest()
            paid = client.post("/api/payment/robokassa/result",
                               data={"OutSum": "20.00", "InvId": str(order_id),
                                     "SignatureValue": signature})
            assert paid.status_code == 200, paid.text

        order_ids[5] = order_ids[5]  # unpaid by design

        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            for index in (1, 2, 3, 4):
                conn.execute("UPDATE orders SET created_at=? WHERE id=?",
                             (iso(now - timedelta(hours=6 - index)), order_ids[index]))
            conn.commit()

            def job_row(index: int, kind: str):
                return conn.execute(
                    "SELECT * FROM delivery_jobs WHERE order_id=? AND kind=?",
                    (order_ids[index], kind)).fetchone()

            # Order 2: an in-flight file job holding a live lease.
            conn.execute(
                "UPDATE delivery_jobs SET state='processing', attempts=1, lease_until=? "
                "WHERE order_id=? AND kind='file'",
                (iso(now + timedelta(minutes=3)), order_ids[2]))

            # Order 3: delivered and fully cleaned, with durable deletion evidence.
            # Both the file and the info attachment must be gone from disk,
            # otherwise the verifier rightly reports a contradictory cleanup.
            cleaned_paths = [
                row["local_path"] for row in conn.execute(
                    "SELECT local_path FROM delivery_jobs WHERE order_id=? AND kind IN ('file','info')",
                    (order_ids[3],)).fetchall() if row["local_path"]
            ]
            conn.execute(
                "UPDATE delivery_jobs SET state='delivered', attempts=1, delivered_at=?, "
                "cleanup_state='completed', cleanup_phase='deleted', local_deleted_at=? "
                "WHERE order_id=? AND kind IN ('file','info')",
                (iso(now - timedelta(minutes=20)), iso(now - timedelta(minutes=10)),
                 order_ids[3]))
            for local_path in cleaned_paths:
                (uploads_dir / Path(local_path).name).unlink(missing_ok=True)

            # Order 4: a file job that failed after two attempts, source retained.
            conn.execute(
                "UPDATE delivery_jobs SET state='failed', attempts=2, last_error=? "
                "WHERE order_id=? AND kind='file'",
                ("synthetic transport failure", order_ids[4]))

            outbox_snapshot: Dict[int, str] = {}
            if with_outbox:
                for index, state in OUTBOX_TARGET.items():
                    accepted = iso(now) if state == "sent" else None
                    existing = conn.execute(
                        "SELECT 1 FROM paid_email_outbox WHERE order_id=?",
                        (order_ids[index],)).fetchone()
                    if existing:
                        conn.execute(
                            "UPDATE paid_email_outbox SET state=?, attempts=1, updated_at=?, "
                            "accepted_at=?, last_error=? WHERE order_id=?",
                            (state, iso(now), accepted,
                             None if state == "sent" else "synthetic ambiguity",
                             order_ids[index]))
                    else:
                        conn.execute(
                            "INSERT INTO paid_email_outbox (order_id, state, attempts, "
                            "updated_at, accepted_at, last_error) VALUES (?,?,?,?,?,?)",
                            (order_ids[index], state, 1, iso(now), accepted,
                             None if state == "sent" else "synthetic ambiguity"))
                    outbox_snapshot[order_ids[index]] = state
            if not with_outbox:
                # Model a pre-PR #26 backup: the table must be genuinely absent,
                # which is what the gate's --require-outbox refuses.
                conn.execute("DROP TABLE IF EXISTS paid_email_outbox")
            conn.commit()
        finally:
            conn.close()

        files = {name: uploads_dir / name for name in
                 (p.name for p in uploads_dir.iterdir() if p.is_file())}
        return {
            "order_ids": order_ids,
            "orders": list(order_ids.values()),
            "paid": [order_ids[i] for i in (1, 2, 3, 4)],
            "unpaid": [order_ids[5]],
            "outbox": outbox_snapshot if with_outbox else {},
            "with_outbox": with_outbox,
            "uploads": {name: path.read_bytes() for name, path in files.items()},
            "now": iso(now),
        }
    finally:
        main.DB_PATH, main.UPLOAD_DIR = original
        for p, approved in original_approval.items():
            if approved is not None:
                consents.DOCUMENTS[p]["approved"] = approved
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def make_uploads_tar(uploads_dir: Path, tar_path: Path,
                     member_names: Optional[Iterable[str]] = None) -> None:
    names = list(member_names) if member_names is not None else None
    with tarfile.open(tar_path, "w:gz") as tar:
        info = tarfile.TarInfo("uploads")
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        tar.addfile(info)
        for item in sorted(Path(uploads_dir).iterdir()):
            if not item.is_file():
                continue
            if names is not None and item.name not in names:
                continue
            tar.add(str(item), arcname=f"uploads/{item.name}")


def write_manifests(
    backup_dir: Path,
    *,
    created: Optional[datetime] = None,
    orders_count: Optional[int] = None,
    verification: str = "PASS",
    corrupt_checksum_for: Optional[str] = None,
) -> None:
    from backup_safety import sha256_file

    db, tar = backup_dir / "orders.db", backup_dir / "uploads.tar.gz"
    entries = {"orders.db": sha256_file(db), "uploads.tar.gz": sha256_file(tar)}
    if corrupt_checksum_for:
        entries[corrupt_checksum_for] = "0" * 64
    (backup_dir / "manifest.sha256").write_text(
        "".join(f"{entries[name]}  {name}\n" for name in entries), encoding="utf-8")

    if orders_count is None:
        conn = sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True)
        try:
            orders_count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        finally:
            conn.close()

    (backup_dir / "backup_manifest.json").write_text(json.dumps({
        "timestamp": iso(created or datetime.now(timezone.utc)),
        "application_id": 1346650441,
        "application_id_tag": "PDAI",
        "orders_count": orders_count,
        "orders_db_sha256": entries["orders.db"],
        "uploads_tar_sha256": entries["uploads.tar.gz"],
        "recovery_verification_result": verification,
    }, indent=2), encoding="utf-8")


def make_backup_set(
    root: Path,
    *,
    name: str = "20260101T000000Z",
    with_outbox: bool = True,
    created: Optional[datetime] = None,
    verification: str = "PASS",
    corrupt_db: bool = False,
    omit: Iterable[str] = (),
    corrupt_checksum_for: Optional[str] = None,
) -> tuple[Path, Dict[str, Any]]:
    """Build one complete backup set and return ``(backup_dir, description)``."""
    root = Path(root)
    backup_dir = root / name
    backup_dir.mkdir(parents=True, exist_ok=True)
    source = root / f"_src-{name}"
    source.mkdir(parents=True, exist_ok=True)

    info = build_database(source / "orders.db", source / "uploads",
                          with_outbox=with_outbox, now=created)

    shutil.copy2(source / "orders.db", backup_dir / "orders.db")
    if corrupt_db:
        with (backup_dir / "orders.db").open("r+b") as handle:
            handle.seek(0)
            handle.write(b"CORRUPTED-NOT-A-DATABASE-FILE-AT-ALL")
    make_uploads_tar(source / "uploads", backup_dir / "uploads.tar.gz")
    write_manifests(
        backup_dir,
        created=created or datetime.now(timezone.utc),
        orders_count=len(info["orders"]),
        verification=verification,
        corrupt_checksum_for=corrupt_checksum_for,
    )

    for missing in omit:
        target = backup_dir / missing
        if target.exists():
            target.unlink()
    return backup_dir, info


def extract_uploads(backup_dir: Path, target_dir: Path) -> Path:
    """Safely extract the upload archive and return the ``uploads`` directory."""
    target_dir = Path(target_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(Path(backup_dir) / "uploads.tar.gz", "r:gz") as tar:
        for member in tar.getmembers():
            destination = (target_dir / member.name).resolve()
            if destination != target_dir and target_dir not in destination.parents:
                raise RuntimeError(f"unsafe member: {member.name}")
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            elif member.isreg():
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is not None:
                    with destination.open("wb") as handle:
                        shutil.copyfileobj(source, handle)
    return target_dir / "uploads"
