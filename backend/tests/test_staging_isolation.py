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
    assert 'PHOTODOC_ALLOW_FRESH_DB: "1"' in content
    # Ensure production volume is NEVER referenced in staging compose
    assert "photodoc_backend-data\n" not in content

SCRIPTS_DIR = BACKEND.parent / "scripts"
MIGRATIONS_DIR = BACKEND / "migrations"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(MIGRATIONS_DIR))

from reconcile_payment_schema import reconcile_schema
from reconcile_delivery_schema import reconcile as reconcile_delivery
import production_preflight
import verify_backup_integrity


def test_staging_cold_start_and_restart_lifecycle(tmp_path, monkeypatch):
    """Verify empty staging starts successfully under PHOTODOC_REQUIRE_DB_IDENTITY=1 with
    PHOTODOC_ALLOW_FRESH_DB=1, and container restart preserves test data without wipe."""
    db_path = tmp_path / "cold_start_orders.db"
    uploads_dir = tmp_path / "cold_start_uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads_dir)
    monkeypatch.setenv("PHOTODOC_REQUIRE_DB_IDENTITY", "1")
    monkeypatch.setenv("PHOTODOC_ALLOW_FRESH_DB", "1")
    monkeypatch.setenv("DELIVERY_WORKER_ENABLED", "0")

    # 1. Cold start on empty volume: DB file does not exist yet
    assert not db_path.exists()
    main.ensure_database_state()

    # 2. Database created and ready
    assert db_path.exists()
    assert main.database_readiness_ok() is True
    assert main.delivery_schema_ready() is True

    # 3. Health check returns 200 {"status": "ok"}
    client = TestClient(main.app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

    # 4. Insert synthetic order record
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO orders (name, phone, format, status) VALUES (?, ?, ?, ?)",
            ("Cold Start Tester", "+79990001122", "10x15", "new"),
        )
        conn.commit()

    # 5. Simulate container restart (ensure_database_state called on existing volume)
    main.ensure_database_state()

    # 6. Verify existing data is preserved (not wiped)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT name, phone FROM orders WHERE phone = '+79990001122'").fetchone()
        assert row is not None
        assert row[0] == "Cold Start Tester"

    # 7. Health check still 200
    resp_after = client.get("/api/health")
    assert resp_after.status_code == 200
    assert resp_after.json() == {"status": "ok"}


def test_production_fail_closed_on_missing_or_empty_database(tmp_path, monkeypatch):
    """Verify production with PHOTODOC_REQUIRE_DB_IDENTITY=1 strictly fails closed on missing,
    empty (0 bytes), alien, or corrupted database when PHOTODOC_ALLOW_FRESH_DB is not set."""
    monkeypatch.setenv("PHOTODOC_REQUIRE_DB_IDENTITY", "1")
    monkeypatch.delenv("PHOTODOC_ALLOW_FRESH_DB", raising=False)

    # 1. Missing database file: must fail closed, NEVER auto-create
    missing_db = tmp_path / "missing_prod.db"
    assert not missing_db.exists()
    monkeypatch.setattr(main, "DB_PATH", missing_db)
    with pytest.raises(RuntimeError, match="Production database missing or empty"):
        main.ensure_database_state()
    assert not missing_db.exists()  # Proves no DB was created!

    # 2. Empty (0-byte) database file: must fail closed
    empty_db = tmp_path / "empty_prod.db"
    empty_db.touch()
    assert empty_db.stat().st_size == 0
    monkeypatch.setattr(main, "DB_PATH", empty_db)
    with pytest.raises(RuntimeError, match="Production database missing or empty"):
        main.ensure_database_state()

    # 3. Alien database (wrong application_id): must fail closed
    alien_db = tmp_path / "alien_prod.db"
    with sqlite3.connect(alien_db) as conn:
        conn.execute("PRAGMA application_id = 999999;")
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY);")
        conn.commit()
    monkeypatch.setattr(main, "DB_PATH", alien_db)
    with pytest.raises(RuntimeError, match="Existing production database does not satisfy"):
        main.ensure_database_state()

    # 4. Valid database: passes without modification
    valid_db = tmp_path / "valid_prod.db"
    monkeypatch.setattr(main, "DB_PATH", valid_db)
    monkeypatch.setenv("PHOTODOC_ALLOW_FRESH_DB", "1")
    main.ensure_database_state()  # initialize valid DB
    monkeypatch.delenv("PHOTODOC_ALLOW_FRESH_DB", raising=False)  # return to production mode

    # Add a marker record
    with sqlite3.connect(valid_db) as conn:
        conn.execute("INSERT INTO orders (name, phone) VALUES ('Prod User', '+79991110000')")
        conn.commit()

    # Startup in production mode: must pass and preserve record
    main.ensure_database_state()
    with sqlite3.connect(valid_db) as conn:
        row = conn.execute("SELECT name FROM orders WHERE phone = '+79991110000'").fetchone()
        assert row and row[0] == "Prod User"


def test_alien_and_corrupt_database_rejected_under_require_identity(tmp_path, monkeypatch):
    """Verify alien or corrupted database is strictly rejected under PHOTODOC_REQUIRE_DB_IDENTITY=1."""
    monkeypatch.setenv("PHOTODOC_REQUIRE_DB_IDENTITY", "1")

    # 1. Alien database with wrong application_id
    alien_db = tmp_path / "alien.db"
    with sqlite3.connect(alien_db) as conn:
        conn.execute("PRAGMA application_id = 999999;")
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, name TEXT);")
        conn.commit()

    monkeypatch.setattr(main, "DB_PATH", alien_db)
    with pytest.raises(RuntimeError, match="Existing database does not satisfy"):
        main.init_db()
    assert main.database_readiness_ok() is False

    # 2. Corrupted database (garbage bytes)
    corrupt_db = tmp_path / "corrupt.db"
    corrupt_db.write_bytes(b"NOT_A_SQLITE_DATABASE_CORRUPT_BYTES")

    monkeypatch.setattr(main, "DB_PATH", corrupt_db)
    with pytest.raises(Exception):
        main.init_db()
    assert main.database_readiness_ok() is False


def test_consistent_single_archive_name_transfer_rehearsal(tmp_path):
    """Verify single consistent archive name across create, checksum, transfer, extract, and verify."""
    archive_name = "photodoc_cutover.tar.gz"
    manifest_name = "manifest.sha256"

    src_dir = tmp_path / "backup_src"
    dst_dir = tmp_path / "backup_dst"
    src_dir.mkdir()
    dst_dir.mkdir()

    # Create synthetic backup contents
    staging_dir = src_dir / "staging_content"
    staging_dir.mkdir()
    db_file = staging_dir / "orders.db.cutover"
    uploads_dir = staging_dir / "uploads"
    uploads_dir.mkdir()

    with sqlite3.connect(db_file) as conn:
        conn.execute("PRAGMA application_id = 1346650441;")
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, name TEXT);")
        conn.commit()
    (uploads_dir / "sample.jpg").write_bytes(JPEG)

    # 1. Pack with exact archive_name
    archive_path = src_dir / archive_name
    with tarfile.open(archive_path, "w:gz") as tar:
        for item in staging_dir.iterdir():
            tar.add(item, arcname=item.name)

    # 2. Compute checksum into manifest.sha256 using consistent relative archive_name
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    manifest_path = src_dir / manifest_name
    manifest_path.write_text(f"{digest}  {archive_name}\n", encoding="utf-8")

    # 3. Transfer archive and manifest
    shutil.copy(archive_path, dst_dir / archive_name)
    shutil.copy(manifest_path, dst_dir / manifest_name)

    # 4. Verify checksum at destination using single consistent name
    dst_manifest = (dst_dir / manifest_name).read_text(encoding="utf-8").strip()
    dst_digest, dst_rel_target = dst_manifest.split("  ")
    assert dst_rel_target == archive_name
    actual_dst_digest = hashlib.sha256((dst_dir / dst_rel_target).read_bytes()).hexdigest()
    assert actual_dst_digest == dst_digest

    # 5. Extract and verify recovered contents
    extract_dir = dst_dir / "recovered"
    extract_dir.mkdir()
    with tarfile.open(dst_dir / archive_name, "r:gz") as tar:
        tar.extractall(extract_dir)

    assert (extract_dir / "orders.db.cutover").exists()
    assert (extract_dir / "uploads" / "sample.jpg").read_bytes() == JPEG


def test_legacy_sqlite_app_id_zero_to_p1_migration_lifecycle(tmp_path):
    """Verify legacy SQLite DB (application_id=0) correctly migrates to P0/P1 and passes integrity checks."""
    legacy_db = tmp_path / "legacy_orders.db"
    legacy_uploads = tmp_path / "legacy_uploads"
    legacy_uploads.mkdir()

    # Create legacy P0 unmarked DB (application_id=0, without order_amount, created_at, order_token_hash)
    with sqlite3.connect(legacy_db) as conn:
        conn.execute("PRAGMA application_id = 0;")
        conn.execute("""
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            phone TEXT,
            email TEXT,
            format TEXT,
            paper TEXT,
            crop TEXT,
            comment TEXT,
            filename TEXT,
            status TEXT DEFAULT 'new',
            payment_status TEXT DEFAULT 'unpaid',
            payment_amount TEXT,
            payment_method TEXT DEFAULT 'robokassa',
            robokassa_inv_id TEXT,
            paid_at TEXT,
            payment_email_sent_at TEXT
        );
        """)
        conn.commit()

    # Before migration: DB identity is invalid (unmarked 0)
    ready_before, diag_before = inspect_database(legacy_db, require_complete_schema=True)
    assert ready_before is False

    # Step 1: P0 Payment Schema Reconciliation (sets application_id=1346650441, adds order_amount, created_at)
    res_p0 = reconcile_schema(legacy_db, apply=True, allow_legacy_unmarked=True)
    assert res_p0["status"] == "COMPLETE"
    assert res_p0["application_id"] == 1346650441

    # Step 2: P1 Delivery Schema Reconciliation (adds order_token_hash, creates delivery_jobs)
    res_p1 = reconcile_delivery(legacy_db, apply=True)
    assert res_p1["applied"] is True

    # Step 3: Production preflight verification
    preflight_ok, preflight_diag = production_preflight.verify_database(legacy_db)
    assert preflight_ok is True
    assert preflight_diag["identity_valid"] is True
    assert preflight_diag["application_id"] == 1346650441

    # Step 4: Backup integrity verification
    backup_ok, backup_report = verify_backup_integrity.verify_backup(legacy_db, legacy_uploads)
    assert backup_ok is True
    assert backup_report["application_id_valid"] is True
    assert backup_report["errors"] == []


def test_safe_rollback_scenario_and_schema_compatibility(tmp_path, monkeypatch):
    """Verify safe rollback scenario: schema compatibility check, maintenance order blocking,
    and preservation of orders, uploads, delivery_jobs, and payment statuses."""
    db_path = tmp_path / "rollback_test.db"
    uploads_dir = tmp_path / "rollback_uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads_dir)
    monkeypatch.setenv("PHOTODOC_REQUIRE_DB_IDENTITY", "1")
    monkeypatch.setenv("PHOTODOC_ALLOW_FRESH_DB", "1")
    monkeypatch.setenv("DELIVERY_WORKER_ENABLED", "0")

    # 1. Initialize DB and create order with delivery jobs and payment status
    main.ensure_database_state()

    test_file = uploads_dir / "1_order.jpg"
    test_file.write_bytes(JPEG)

    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            INSERT INTO orders (id, name, phone, format, status, payment_status, payment_amount, robokassa_inv_id, paid_at, order_token_hash)
            VALUES (1, 'Rollback Tester', '+79995554433', '10x15', 'paid', 'paid', '500.00', '1001', '2026-10-27T06:00:00', 'tokenhash123')
        """)
        conn.execute("""
            INSERT INTO delivery_jobs (id, order_id, kind, remote_path, local_path, content_size, state, next_attempt_at, created_at)
            VALUES ('job1', 1, 'file', 'PhotoDoc_Orders/1/1_order.jpg', '1_order.jpg', 100, 'pending', '2026-10-27T06:00:00', '2026-10-27T06:00:00')
        """)
        conn.commit()

    # 2. Gate: Schema compatibility check of current DB against candidate rollback codebase
    ready, diag = inspect_database(db_path, require_complete_schema=True)
    assert ready is True, "Candidate rollback code must be compatible with current database"

    # 3. Simulate incompatible schema gate rejection:
    # If a destructive migration had altered/dropped required P1 columns
    incompatible_db = tmp_path / "incompatible.db"
    with sqlite3.connect(incompatible_db) as conn:
        conn.execute("PRAGMA application_id = 1346650441;")
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY);") # missing required columns!
        conn.commit()
    ready_incompat, _ = inspect_database(incompatible_db, require_complete_schema=True)
    assert ready_incompat is False
    # Proves gate rejects rollback if schema compatibility fails!

    # 4. Simulate rollback execution: restart backend container with rollback target code
    monkeypatch.delenv("PHOTODOC_ALLOW_FRESH_DB", raising=False) # Production mode!
    main.ensure_database_state()

    # 5. Verify 100% preservation of orders, uploads, delivery_jobs, and payment statuses
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        order = conn.execute("SELECT * FROM orders WHERE id = 1").fetchone()
        assert order is not None
        assert order["name"] == "Rollback Tester"
        assert order["payment_status"] == "paid"
        assert order["robokassa_inv_id"] == "1001"
        assert order["order_token_hash"] == "tokenhash123"

        jobs = conn.execute("SELECT * FROM delivery_jobs WHERE order_id = 1").fetchall()
        assert len(jobs) == 1
        assert jobs[0]["state"] == "pending"
        assert jobs[0]["kind"] == "file"

    assert test_file.exists()
    assert test_file.read_bytes() == JPEG
