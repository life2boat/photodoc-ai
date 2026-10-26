import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import main
from db_contract import inspect_database

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"


def test_yandex_disk_root_default_and_override():
    """Verify Yandex Disk root defaults to PhotoDoc_Orders and honors YANDEX_DISK_ROOT_DIR."""
    # 1. Default when unset
    with patch.dict(os.environ, {}, clear=True):
        assert main.get_yandex_disk_root() == "PhotoDoc_Orders"

    # 2. Staging override
    with patch.dict(os.environ, {"YANDEX_DISK_ROOT_DIR": "PhotoDoc_Staging"}):
        assert main.get_yandex_disk_root() == "PhotoDoc_Staging"

    # 3. Leading and trailing slashes stripped safely
    with patch.dict(os.environ, {"YANDEX_DISK_ROOT_DIR": "/PhotoDoc_Staging/"}):
        assert main.get_yandex_disk_root() == "PhotoDoc_Staging"

    # 4. Whitespace stripped safely
    with patch.dict(os.environ, {"YANDEX_DISK_ROOT_DIR": "   PhotoDoc_Staging   "}):
        assert main.get_yandex_disk_root() == "PhotoDoc_Staging"

    # 5. Empty or slash-only fallback to PhotoDoc_Orders
    with patch.dict(os.environ, {"YANDEX_DISK_ROOT_DIR": "///"}):
        assert main.get_yandex_disk_root() == "PhotoDoc_Orders"


def test_order_creation_with_staging_yandex_root(tmp_path, monkeypatch):
    """Verify order delivery jobs use the staging Yandex root when configured."""
    db_path = tmp_path / "staging_orders.db"
    uploads_dir = tmp_path / "staging_uploads"
    uploads_dir.mkdir(parents=True)

    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads_dir)
    monkeypatch.setenv("ROBOKASSA_MERCHANT_LOGIN", "test-login")
    monkeypatch.setenv("ROBOKASSA_PASSWORD1", "test-pass1")
    monkeypatch.setenv("ROBOKASSA_PASSWORD2", "test-pass2")
    monkeypatch.setenv("DELIVERY_WORKER_ENABLED", "0")
    monkeypatch.setenv("YANDEX_DISK_ROOT_DIR", "PhotoDoc_Staging")

    main.init_db()

    client = TestClient(main.app)
    resp = client.post(
        "/api/order",
        data={"name": "Staging Tester", "phone": "+79991112233", "format": "10x15"},
        files=[("files", ("photo1.jpg", JPEG, "image/jpeg"))],
    )
    assert resp.status_code == 200
    order_id = resp.json()["order_id"]

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        jobs = conn.execute("SELECT * FROM delivery_jobs WHERE order_id = ?", (order_id,)).fetchall()
        assert len(jobs) == 3  # 1 file, 1 info, 1 smtp

        remote_jobs = [j for j in jobs if j["kind"] in ("file", "info")]
        for job in remote_jobs:
            assert job["remote_path"].startswith("PhotoDoc_Staging/")
            assert not job["remote_path"].startswith("PhotoDoc_Orders/")


def test_fresh_staging_database_initialization(tmp_path, monkeypatch):
    """Verify fresh staging database has PDAI identity and full P1 schema."""
    db_path = tmp_path / "fresh_staging.db"
    monkeypatch.setattr(main, "DB_PATH", db_path)

    main.init_db()

    with sqlite3.connect(db_path) as conn:
        app_id = conn.execute("PRAGMA application_id").fetchone()[0]
        assert app_id == 1346650441

        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "orders" in tables
        assert "delivery_jobs" in tables

    ready, diagnostics = inspect_database(db_path, require_complete_schema=True)
    assert ready is True
    assert diagnostics["identity_valid"] is True
    assert diagnostics["application_id"] == 1346650441


def test_portable_backup_transfer_rehearsal(tmp_path):
    """Reproduce full archive -> transfer -> checksum -> extract -> verify lifecycle with relative paths."""
    src_dir = tmp_path / "source_host"
    dst_dir = tmp_path / "dest_host"
    src_dir.mkdir()
    dst_dir.mkdir()

    bundle_dir = src_dir / "bundle"
    bundle_dir.mkdir()
    uploads_dir = bundle_dir / "uploads"
    uploads_dir.mkdir()

    # Create synthetic database
    db_file = bundle_dir / "orders.db"
    with sqlite3.connect(db_file) as conn:
        conn.execute("PRAGMA application_id = 1346650441;")
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, name TEXT);")
        conn.commit()

    # Create synthetic uploads
    photo1 = uploads_dir / "1_test.jpg"
    photo1.write_bytes(JPEG)

    # Generate internal manifest with portable relative POSIX paths
    manifest_lines = []
    for item in [db_file, photo1]:
        rel_name = item.relative_to(bundle_dir).as_posix()
        digest = hashlib.sha256(item.read_bytes()).hexdigest()
        manifest_lines.append(f"{digest}  {rel_name}")
    (bundle_dir / "manifest.sha256").write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")

    # Pack archive
    archive_file = src_dir / "photodoc_cutover.tar.gz"
    with tarfile.open(archive_file, "w:gz") as tar:
        for f in bundle_dir.iterdir():
            tar.add(f, arcname=f.name)

    # Generate external checksum with relative basename (portable across paths)
    archive_digest = hashlib.sha256(archive_file.read_bytes()).hexdigest()
    checksum_file = src_dir / "photodoc_cutover.tar.gz.sha256"
    checksum_file.write_text(f"{archive_digest}  photodoc_cutover.tar.gz\n", encoding="utf-8")

    # --- SIMULATE TRANSFER TO COMPLETELY DIFFERENT DIRECTORY ---
    shutil.copy(archive_file, dst_dir / "photodoc_cutover.tar.gz")
    shutil.copy(checksum_file, dst_dir / "photodoc_cutover.tar.gz.sha256")

    # 1. Verify bundle hash on destination using relative filename
    dst_checksum_text = (dst_dir / "photodoc_cutover.tar.gz.sha256").read_text(encoding="utf-8").strip()
    expected_hash, rel_target = dst_checksum_text.split("  ")
    actual_hash = hashlib.sha256((dst_dir / rel_target).read_bytes()).hexdigest()
    assert actual_hash == expected_hash

    # 2. Extract into destination directory
    extract_dir = dst_dir / "extracted"
    extract_dir.mkdir()
    with tarfile.open(dst_dir / "photodoc_cutover.tar.gz", "r:gz") as tar:
        tar.extractall(extract_dir)

    # 3. Verify internal manifest
    manifest_content = (extract_dir / "manifest.sha256").read_text(encoding="utf-8").splitlines()
    for line in manifest_content:
        if not line:
            continue
        h, rel_p = line.split("  ")
        target_p = extract_dir / rel_p
        assert target_p.exists()
        assert hashlib.sha256(target_p.read_bytes()).hexdigest() == h


def test_staging_compose_file_structure():
    """Verify docker-compose.staging.yml specifies isolated volumes, networks, and environment."""
    compose_path = Path(__file__).resolve().parent.parent.parent / "docker-compose.staging.yml"
    assert compose_path.exists()
    content = compose_path.read_text(encoding="utf-8")

    assert "photodoc-staging-backend" in content
    assert "photodoc-staging-frontend" in content
    assert "photodoc_staging_backend-data" in content
    assert "photodoc_staging_uploads" in content
    assert "photodoc_staging_network" in content
    assert 'DELIVERY_WORKER_ENABLED: "0"' in content
    assert 'ROBOKASSA_IS_TEST: "1"' in content
    assert 'YANDEX_DISK_ROOT_DIR: "PhotoDoc_Staging"' in content
    # Ensure production volume is NEVER referenced in staging compose
    assert "photodoc_backend-data\n" not in content
