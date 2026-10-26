"""
PhotoDoc AI — Backup Integrity & Storage Rehearsal Test Suite.

Validates:
1. Reproduction of false failure in naive filename-matching on real P1 orders.
2. P0 historical orders with existing local files.
3. P0 historical orders with absent local copies (unretained files).
4. P1 orders with UUID-stored filenames.
5. Two attachments with duplicate original filenames in the same order.
6. Detection of missing P1 files.
7. Detection of corrupted P1 files (SHA-256 mismatch).
8. Detection of truncated P1 files (size mismatch).
9. Path traversal protection against directory escape attempts.
10. Multi-order, multi-attachment end-to-end backup, extraction, and verification lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
SCRIPTS_DIR = REPO_DIR / "scripts"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import main  # noqa: E402
from db_contract import APPLICATION_ID  # noqa: E402
from verify_backup_integrity import sanitize_filename, verify_backup  # noqa: E402

JPEG = b"\xff\xd8\xffsynthetic-jpeg-data-for-testing"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRsynthetic-png-data"

P0_COLUMNS = (
    "id", "name", "phone", "email", "format", "paper", "crop", "comment",
    "filename", "status", "payment_status", "order_amount", "payment_amount",
    "payment_method", "robokassa_inv_id", "paid_at", "payment_email_sent_at",
    "created_at",
)


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    db = tmp_path / "orders.db"
    uploads = tmp_path / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(main, "DB_PATH", db)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads)
    monkeypatch.setenv("ROBOKASSA_MERCHANT_LOGIN", "test-merchant")
    monkeypatch.setenv("ROBOKASSA_PASSWORD1", "test-pass-1")
    monkeypatch.setenv("ROBOKASSA_PASSWORD2", "test-pass-2")
    monkeypatch.setenv("DELIVERY_WORKER_ENABLED", "0")

    main.init_db()
    client = TestClient(main.app)
    yield client, db, uploads


def create_p0_order(db_path: Path, order_id: int, original_filename: str = "photo.jpg"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO orders (id, name, phone, email, format, paper, crop, comment, filename, "
        "status, payment_status, order_amount, payment_amount, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', 'paid', '20.00', '20.00', '2026-01-01T00:00:00')",
        (order_id, f"Customer {order_id}", "+79991112233", "cust@example.com", "10x15",
         "Матовая", "Без полей", "P0 order", original_filename),
    )
    conn.commit()
    conn.close()


def test_reproduce_old_check_false_failure_on_real_p1_order(test_env):
    """
    1. Воспроизвести ложный отказ существующей проверки на реальном механизме создания P1-заказов.
    """
    client, db_path, uploads_dir = test_env

    resp = client.post(
        "/api/order",
        data={"name": "Client One", "phone": "+79991112233", "format": "10x15", "paper": "Матовая", "crop": "Без полей"},
        files=[("files", ("vacation_photo.jpg", JPEG, "image/jpeg"))],
    )
    assert resp.status_code == 200
    order_id = resp.json()["order_id"]

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        assert order["filename"] == "vacation_photo.jpg"

    # In uploads, the file is saved under UUID-name (order_id_uuid.jpg), NOT vacation_photo.jpg
    files_in_uploads = list(uploads_dir.glob("*.jpg"))
    assert len(files_in_uploads) == 1
    stored_name = files_in_uploads[0].name
    assert stored_name != "vacation_photo.jpg"
    assert stored_name.startswith(f"{order_id}_")

    # OLD NAIVE CHECK: looks for uploads/<orders.filename>
    naive_target = uploads_dir / order["filename"]
    assert not naive_target.is_file(), "Old naive check falsely expects vacation_photo.jpg"

    # NEW HARDENED VERIFIER: checks delivery_jobs linkage, size and sha256
    is_valid, report = verify_backup(db_path, uploads_dir)
    assert is_valid is True
    assert report["p1_orders_count"] == 1
    assert report["p1_verified_files"] == 1
    assert report["p1_verified_info"] == 1
    assert len(report["p1_missing_files"]) == 0


def test_p0_order_with_existing_file(tmp_path):
    """P0-заказ с существующим файлом."""
    db_path = tmp_path / "orders.db"
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()

    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    cols_def = ", ".join(f"{col} TEXT" if col != "id" else "id INTEGER PRIMARY KEY AUTOINCREMENT" for col in P0_COLUMNS)
    conn.execute(f"CREATE TABLE orders ({cols_def})")
    conn.close()

    create_p0_order(db_path, order_id=1, original_filename="passport.jpg")

    # P0 storage convention: {order_id}_{safe_filename}
    p0_file = uploads_dir / "1_passport.jpg"
    p0_file.write_bytes(JPEG)

    is_valid, report = verify_backup(db_path, uploads_dir)
    assert is_valid is True
    assert report["p0_orders_count"] == 1
    assert report["p0_confirmed_local_files"] == 1
    assert report["p0_unretained_historical_files"] == 0


def test_p0_order_with_missing_local_copy(tmp_path):
    """P0-заказ с отсутствующей локальной копией (не ломает бэкап, но явно фиксируется)."""
    db_path = tmp_path / "orders.db"
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()

    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    cols_def = ", ".join(f"{col} TEXT" if col != "id" else "id INTEGER PRIMARY KEY AUTOINCREMENT" for col in P0_COLUMNS)
    conn.execute(f"CREATE TABLE orders ({cols_def})")
    conn.close()

    create_p0_order(db_path, order_id=2, original_filename="archived_1990.jpg")

    # Local file was cleaned up on disk long ago
    is_valid, report = verify_backup(db_path, uploads_dir)
    assert is_valid is True
    assert report["p0_orders_count"] == 1
    assert report["p0_confirmed_local_files"] == 0
    assert report["p0_unretained_historical_files"] == 1

    # If explicitly required, it reports error
    is_valid_strict, report_strict = verify_backup(db_path, uploads_dir, require_p0_local=True)
    assert is_valid_strict is False
    assert any("missing local file" in e for e in report_strict["errors"])


def test_p1_order_uuid_and_duplicate_original_names(test_env):
    """
    P1-заказ с UUID-именами файлов и двумя вложениями с одинаковым исходным именем.
    """
    client, db_path, uploads_dir = test_env

    resp = client.post(
        "/api/order",
        data={"name": "Client Two", "phone": "+79992223344", "format": "10x15"},
        files=[
            ("files", ("photo.jpg", JPEG, "image/jpeg")),
            ("files", ("photo.jpg", PNG, "image/png")),
        ],
    )
    assert resp.status_code == 200
    order_id = resp.json()["order_id"]

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        jobs = conn.execute("SELECT * FROM delivery_jobs WHERE order_id = ? AND kind = 'file'", (order_id,)).fetchall()
        assert len(jobs) == 2
        # Both jobs have different stored names and different hashes/sizes
        assert jobs[0]["local_path"] != jobs[1]["local_path"]
        assert jobs[0]["content_sha256"] != jobs[1]["content_sha256"]

    is_valid, report = verify_backup(db_path, uploads_dir)
    assert is_valid is True
    assert report["p1_verified_files"] == 2
    assert report["p1_verified_info"] == 1


def test_p1_missing_file_detection(test_env):
    """Отсутствующий P1-файл."""
    client, db_path, uploads_dir = test_env

    resp = client.post(
        "/api/order",
        data={"name": "Client Three", "phone": "+79993334455", "format": "10x15"},
        files=[("files", ("one.jpg", JPEG, "image/jpeg"))],
    )
    assert resp.status_code == 200

    # Delete the stored photo from disk
    stored_photos = list(uploads_dir.glob("*.jpg"))
    assert len(stored_photos) == 1
    stored_photos[0].unlink()

    is_valid, report = verify_backup(db_path, uploads_dir)
    assert is_valid is False
    assert len(report["p1_missing_files"]) == 1
    assert report["p1_missing_files"][0]["kind"] == "file"


def test_p1_wrong_sha256_detection(test_env):
    """P1-файл с неправильным SHA-256."""
    client, db_path, uploads_dir = test_env

    resp = client.post(
        "/api/order",
        data={"name": "Client Four", "phone": "+79994445566", "format": "10x15"},
        files=[("files", ("important.jpg", JPEG, "image/jpeg"))],
    )
    assert resp.status_code == 200

    stored_photos = list(uploads_dir.glob("*.jpg"))
    # Corrupt content while keeping same length
    orig_data = stored_photos[0].read_bytes()
    corrupted_data = orig_data[:-1] + b"\x00"
    stored_photos[0].write_bytes(corrupted_data)

    is_valid, report = verify_backup(db_path, uploads_dir)
    assert is_valid is False
    assert len(report["p1_checksum_mismatches"]) == 1


def test_p1_wrong_size_detection(test_env):
    """P1-файл с неправильным размером."""
    client, db_path, uploads_dir = test_env

    resp = client.post(
        "/api/order",
        data={"name": "Client Five", "phone": "+79995556677", "format": "10x15"},
        files=[("files", ("image.png", PNG, "image/png"))],
    )
    assert resp.status_code == 200

    stored_pngs = list(uploads_dir.glob("*.png"))
    # Truncate
    stored_pngs[0].write_bytes(b"\x89PNG")

    is_valid, report = verify_backup(db_path, uploads_dir)
    assert is_valid is False
    assert len(report["p1_size_mismatches"]) == 1


def test_path_traversal_escape_attempt(test_env):
    """Попытка выхода за пределы каталога восстановления."""
    client, db_path, uploads_dir = test_env

    resp = client.post(
        "/api/order",
        data={"name": "Client Six", "phone": "+79996667788", "format": "10x15"},
        files=[("files", ("doc.jpg", JPEG, "image/jpeg"))],
    )
    assert resp.status_code == 200
    order_id = resp.json()["order_id"]

    # Inject malicious local_path with ../ traversal
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE delivery_jobs SET local_path = ? WHERE order_id = ? AND kind = 'file'",
            (f"/app/uploads/../../etc/passwd", order_id),
        )

    is_valid, report = verify_backup(db_path, uploads_dir)
    assert is_valid is False
    assert len(report["path_traversal_violations"]) == 1


def test_multi_order_multi_attachment_restoration_lifecycle(tmp_path, monkeypatch):
    """
    Корректное восстановление нескольких заказов и всех их вложений.
    Сохранение связи orders -> delivery_jobs -> uploads.
    """
    live_dir = tmp_path / "live"
    live_db = live_dir / "orders.db"
    live_uploads = live_dir / "uploads"
    live_uploads.mkdir(parents=True)

    backup_root = tmp_path / "backups"
    backup_dir = backup_root / "release_20261026_120000"
    backup_dir.mkdir(parents=True)

    restore_sandbox = tmp_path / "restore_sandbox"
    restore_sandbox.mkdir(parents=True)

    monkeypatch.setattr(main, "DB_PATH", live_db)
    monkeypatch.setattr(main, "UPLOAD_DIR", live_uploads)
    monkeypatch.setenv("ROBOKASSA_MERCHANT_LOGIN", "test-merchant")
    monkeypatch.setenv("ROBOKASSA_PASSWORD1", "test-pass-1")
    monkeypatch.setenv("ROBOKASSA_PASSWORD2", "test-pass-2")
    monkeypatch.setenv("DELIVERY_WORKER_ENABLED", "0")

    main.init_db()

    # 1. Create a historical P0 order with file
    create_p0_order(live_db, order_id=1, original_filename="p0_legacy.jpg")
    (live_uploads / "1_p0_legacy.jpg").write_bytes(JPEG)

    # 2. Create another historical P0 order whose file was not retained
    create_p0_order(live_db, order_id=2, original_filename="lost_legacy.jpg")

    # 3. Create two real P1 orders via TestClient
    client = TestClient(main.app)
    resp3 = client.post(
        "/api/order",
        data={"name": "Alice P1", "phone": "+79991110001", "format": "10x15"},
        files=[("files", ("alice_a.jpg", JPEG, "image/jpeg")), ("files", ("alice_b.png", PNG, "image/png"))],
    )
    assert resp3.status_code == 200

    resp4 = client.post(
        "/api/order",
        data={"name": "Bob P1", "phone": "+79991110002", "format": "3x4"},
        files=[("files", ("passport.jpg", JPEG, "image/jpeg"))],
    )
    assert resp4.status_code == 200

    # 4. Perform SQLite Online Backup and Uploads Tar
    conn = sqlite3.connect(live_db)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")

    backup_db_path = backup_dir / "orders.db"
    backup_conn = sqlite3.connect(backup_db_path)
    with backup_conn:
        conn.backup(backup_conn)
    backup_conn.close()
    conn.close()

    uploads_tar_path = backup_dir / "uploads.tar.gz"
    with tarfile.open(uploads_tar_path, "w:gz") as tar:
        tar.add(live_uploads, arcname="uploads")

    # 5. Extract into isolated sandbox
    shutil.copy2(backup_db_path, restore_sandbox / "orders.db")
    with tarfile.open(uploads_tar_path, "r:gz") as tar:
        tar.extractall(restore_sandbox)

    restored_db = restore_sandbox / "orders.db"
    restored_uploads = restore_sandbox / "uploads"

    # 6. Verify with our tool
    is_valid, report = verify_backup(restored_db, restored_uploads)
    assert is_valid is True
    assert report["orders_count"] == 4
    assert report["p1_orders_count"] == 2
    assert report["p1_verified_files"] == 3  # 2 for Alice, 1 for Bob
    assert report["p1_verified_info"] == 2   # 1 info.txt each
    assert report["p0_orders_count"] == 2
    assert report["p0_confirmed_local_files"] == 1
    assert report["p0_unretained_historical_files"] == 1
    assert len(report["errors"]) == 0


def test_cli_execution_success_and_failure(tmp_path):
    """Тестирование CLI интерфейса verify_backup_integrity.py."""
    import subprocess

    db_path = tmp_path / "orders.db"
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()

    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    cols_def = ", ".join(f"{col} TEXT" if col != "id" else "id INTEGER PRIMARY KEY AUTOINCREMENT" for col in P0_COLUMNS)
    conn.execute(f"CREATE TABLE orders ({cols_def})")
    conn.close()

    script = SCRIPTS_DIR / "verify_backup_integrity.py"

    # Test JSON mode
    proc = subprocess.run(
        [sys.executable, str(script), "--db", str(db_path), "--uploads", str(uploads_dir), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["valid"] is True
    assert data["report"]["sqlite_integrity"] == "ok"
    assert data["report"]["application_id"] == APPLICATION_ID

    # Test Corrupted DB failure
    bad_db = tmp_path / "corrupt.db"
    bad_db.write_bytes(b"corrupt-data")
    proc_bad = subprocess.run(
        [sys.executable, str(script), "--db", str(bad_db), "--uploads", str(uploads_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_bad.returncode == 1
    assert "INTEGRITY_CHECK_FAILED" in proc_bad.stdout


def test_p1_order_without_any_delivery_jobs_fails(test_env):
    """1. P1-заказ без всех delivery_jobs: отказ верификации (не должен считаться P0)."""
    client, db_path, uploads_dir = test_env
    resp = client.post(
        "/api/order",
        data={"name": "Client Lost Jobs", "phone": "+79991112233", "format": "10x15"},
        files=[("files", ("photo.jpg", JPEG, "image/jpeg"))],
    )
    assert resp.status_code == 200
    order_id = resp.json()["order_id"]

    # Delete all delivery jobs for this P1 order
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM delivery_jobs WHERE order_id = ?", (order_id,))

    is_valid, report = verify_backup(db_path, uploads_dir)
    assert is_valid is False
    assert report["p1_orders_count"] == 1
    assert report["p0_orders_count"] == 0  # Crucial: NOT silently reclassified as P0!
    assert any("has NO entries in delivery_jobs" in e for e in report["p1_completeness_errors"])


def test_p1_order_with_missing_single_file_job_fails(test_env):
    """2. P1-заказ с удалённой одной файловой задачей: отказ верификации."""
    client, db_path, uploads_dir = test_env
    resp = client.post(
        "/api/order",
        data={"name": "Client Partial Jobs", "phone": "+79991112233", "format": "10x15"},
        files=[
            ("files", ("one.jpg", JPEG, "image/jpeg")),
            ("files", ("two.png", PNG, "image/png")),
        ],
    )
    assert resp.status_code == 200
    order_id = resp.json()["order_id"]

    # Delete one file job
    with sqlite3.connect(db_path) as conn:
        job_id = conn.execute(
            "SELECT id FROM delivery_jobs WHERE order_id = ? AND kind = 'file' LIMIT 1",
            (order_id,),
        ).fetchone()[0]
        conn.execute("DELETE FROM delivery_jobs WHERE id = ?", (job_id,))

    is_valid, report = verify_backup(db_path, uploads_dir)
    assert is_valid is False
    assert any("file count mismatch" in e for e in report["p1_completeness_errors"])


def test_p1_order_without_info_job_fails(test_env):
    """3. P1-заказ без задачи info: отказ верификации."""
    client, db_path, uploads_dir = test_env
    resp = client.post(
        "/api/order",
        data={"name": "Client No Info", "phone": "+79991112233", "format": "10x15"},
        files=[("files", ("photo.jpg", JPEG, "image/jpeg"))],
    )
    assert resp.status_code == 200
    order_id = resp.json()["order_id"]

    # Delete info job
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM delivery_jobs WHERE order_id = ? AND kind = 'info'", (order_id,))

    is_valid, report = verify_backup(db_path, uploads_dir)
    assert is_valid is False
    assert any("must have exactly 1 'info' job" in e for e in report["p1_completeness_errors"])


def test_orphan_delivery_job_without_order_fails(test_env):
    """4. Задача доставки без существующего заказа: отказ верификации."""
    client, db_path, uploads_dir = test_env
    resp = client.post(
        "/api/order",
        data={"name": "Client Base", "phone": "+79991112233", "format": "10x15"},
        files=[("files", ("photo.jpg", JPEG, "image/jpeg"))],
    )
    assert resp.status_code == 200

    # Insert an orphan delivery job pointing to non-existent order #99999
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO delivery_jobs (id, order_id, kind, state, attempts, next_attempt_at, created_at, local_path, content_sha256, content_size) "
            "VALUES ('orphan-job-id', ?, 'file', 'pending', 0, '2026-01-01', '2026-01-01', '/app/uploads/orphan.jpg', ?, 100)",
            (99999, "a" * 64),
        )

    is_valid, report = verify_backup(db_path, uploads_dir)
    assert is_valid is False
    assert len(report["orphan_delivery_jobs"]) == 1
    assert report["orphan_delivery_jobs"][0]["order_id"] == 99999


def test_p1_job_missing_or_malformed_sha256_fails(test_env):
    """5. Отсутствующий, пустой или некорректный SHA-256: отказ верификации."""
    client, db_path, uploads_dir = test_env
    resp = client.post(
        "/api/order",
        data={"name": "Client Bad Hash", "phone": "+79991112233", "format": "10x15"},
        files=[("files", ("photo.jpg", JPEG, "image/jpeg"))],
    )
    assert resp.status_code == 200
    order_id = resp.json()["order_id"]

    for bad_hash in [None, "", "not-a-hash", "short"]:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE delivery_jobs SET content_sha256 = ? WHERE order_id = ? AND kind = 'file'",
                (bad_hash, order_id),
            )
        is_valid, report = verify_backup(db_path, uploads_dir)
        assert is_valid is False
        assert any(e["field"] == "content_sha256" for e in report["p1_metadata_errors"])


def test_p1_job_missing_or_invalid_size_fails(test_env):
    """6. Отсутствующий, отрицательный или нецелочисленный размер: отказ верификации."""
    client, db_path, uploads_dir = test_env
    resp = client.post(
        "/api/order",
        data={"name": "Client Bad Size", "phone": "+79991112233", "format": "10x15"},
        files=[("files", ("photo.jpg", JPEG, "image/jpeg"))],
    )
    assert resp.status_code == 200
    order_id = resp.json()["order_id"]

    for bad_size in [None, -1, -100]:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE delivery_jobs SET content_size = ? WHERE order_id = ? AND kind = 'file'",
                (bad_size, order_id),
            )
        is_valid, report = verify_backup(db_path, uploads_dir)
        assert is_valid is False
        assert any(e["field"] == "content_size" for e in report["p1_metadata_errors"])


def test_p1_corrupt_content_with_missing_control_metadata_fails(test_env):
    """7. Повреждённое содержимое при отсутствующих контрольных метаданных: отказ верификации."""
    client, db_path, uploads_dir = test_env
    resp = client.post(
        "/api/order",
        data={"name": "Client Corrupt Null Hash", "phone": "+79991112233", "format": "10x15"},
        files=[("files", ("photo.jpg", JPEG, "image/jpeg"))],
    )
    assert resp.status_code == 200
    order_id = resp.json()["order_id"]

    # Corrupt file on disk
    stored_photos = list(uploads_dir.glob("*.jpg"))
    assert len(stored_photos) == 1
    stored_photos[0].write_bytes(b"\x00" * len(JPEG))

    # Strip control metadata from DB
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE delivery_jobs SET content_sha256 = NULL, content_size = NULL WHERE order_id = ? AND kind = 'file'",
            (order_id,),
        )

    # Must fail because control metadata is missing (does NOT mask corruption!)
    is_valid, report = verify_backup(db_path, uploads_dir)
    assert is_valid is False
    assert len(report["p1_metadata_errors"]) >= 1


def test_intact_mixed_p0_p1_database_passes(tmp_path, monkeypatch):
    """8. Неповреждённая смешанная P0/P1-база: успешное прохождение (PASS)."""
    db_path = tmp_path / "mixed.db"
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir(parents=True)

    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads_dir)
    monkeypatch.setenv("ROBOKASSA_MERCHANT_LOGIN", "test-merchant")
    monkeypatch.setenv("ROBOKASSA_PASSWORD1", "test-pass-1")
    monkeypatch.setenv("ROBOKASSA_PASSWORD2", "test-pass-2")
    monkeypatch.setenv("DELIVERY_WORKER_ENABLED", "0")

    main.init_db()

    # P0 order 1 with local file
    create_p0_order(db_path, order_id=1, original_filename="p0_one.jpg")
    (uploads_dir / "1_p0_one.jpg").write_bytes(JPEG)

    # P0 order 2 without local file (unretained)
    create_p0_order(db_path, order_id=2, original_filename="p0_two.jpg")

    # P1 orders created via real API
    client = TestClient(main.app)
    resp3 = client.post(
        "/api/order",
        data={"name": "Alice P1", "phone": "+79991110001", "format": "10x15"},
        files=[("files", ("alice.jpg", JPEG, "image/jpeg"))],
    )
    assert resp3.status_code == 200

    resp4 = client.post(
        "/api/order",
        data={"name": "Bob P1", "phone": "+79991110002", "format": "Polaroid"},
        files=[
            ("files", ("shot1.jpg", JPEG, "image/jpeg")),
            ("files", ("shot2.png", PNG, "image/png")),
        ],
    )
    assert resp4.status_code == 200

    is_valid, report = verify_backup(db_path, uploads_dir)
    assert is_valid is True
    assert report["orders_count"] == 4
    assert report["p0_orders_count"] == 2
    assert report["p1_orders_count"] == 2
    assert report["p0_confirmed_local_files"] == 1
    assert report["p0_unretained_historical_files"] == 1
    assert report["p1_verified_files"] == 3
    assert report["p1_verified_info"] == 2
    assert len(report["p1_completeness_errors"]) == 0
    assert len(report["p1_metadata_errors"]) == 0
    assert len(report["orphan_delivery_jobs"]) == 0
    assert len(report["errors"]) == 0
